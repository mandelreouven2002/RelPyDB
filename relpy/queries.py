from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable
import builtins
import copy
import json
import re

from .exceptions import (
    ColumnNotFoundError,
    QueryError,
    QueryTypeError,
    NoRowsFoundError,
    MultipleRowsFoundError,
)
from .hashing import make_hashable, default_json_encoder
from ._core import core as _c_engine  # the C engine (always present)


# =============================================================================
# Condition plan opcodes
# =============================================================================
#
# In addition to its opaque Python `predicate`, a Condition may carry a
# structured, introspectable `plan`: a small tuple tree describing the
# comparison(s) it performs. When every leaf of a WHERE clause is a structured
# comparison (the common case, e.g. `col("age") > 30`), the optional native
# engine (`relpy._core`) can evaluate the whole tree against raw rows in a tight
# C loop, with no per-row Python call. Anything the plan cannot express (a raw
# `where(lambda ...)`, a `.like()` regex) simply has `plan=None` and falls back
# to the pure-Python predicate. The plan is a pure optimization: results are
# identical whether it is used or not.
#
# A plan node is a tuple whose first element is one of these opcodes:
OP_EQ = 0
OP_NE = 1
OP_GT = 2
OP_GE = 3
OP_LT = 4
OP_LE = 5
OP_IN = 6
OP_BETWEEN = 7        # (OP_BETWEEN, col, low, high, inclusive)
OP_ISNULL = 8
OP_NOTNULL = 9
OP_AND = 10           # (OP_AND, left_node, right_node)
OP_OR = 11            # (OP_OR, left_node, right_node)
OP_NOT = 12           # (OP_NOT, child_node)


# =============================================================================
# Conditions
# =============================================================================

@dataclass(frozen=True)
class Condition:
    """
    Represents a boolean condition that can be evaluated against a row.

    A Condition is not evaluated immediately.
    It is evaluated later by Query when rows are processed.

    Example:
        col("age") >= 18
    """

    predicate: Callable[[dict[str, Any]], bool]
    columns: frozenset[str] = field(default_factory=frozenset)
    description: str = ""
    equality_conditions: tuple[tuple[str, Any], ...] = field(default_factory=tuple)
    # Optional structured plan for native evaluation (see opcodes above).
    # None means "not natively planable"; fall back to `predicate`.
    plan: tuple | None = None

    def __call__(self, row: dict[str, Any]) -> bool:
        """
        Evaluates the condition against a row.
        """

        result = self.predicate(row)

        if type(result) is not bool:
            raise TypeError(
                f"Condition must return bool, got {type(result).__name__}."
            )

        return result

    def __and__(self, other: Condition) -> Condition:
        """
        Combines two conditions using logical AND.

        Usage:
            (col("age") >= 18) & (col("status") == "active")
        """

        other = _ensure_condition(other)

        combined_plan = (
            (OP_AND, self.plan, other.plan)
            if self.plan is not None and other.plan is not None
            else None
        )

        return Condition(
            predicate=lambda row: self(row) and other(row),
            columns=self.columns | other.columns,
            description=f"({self.description}) AND ({other.description})",
            equality_conditions=_merge_equality_conditions(
                self.equality_conditions,
                other.equality_conditions,
            ),
            plan=combined_plan,
        )

    def __or__(self, other: Condition) -> Condition:
        """
        Combines two conditions using logical OR.

        Usage:
            (col("age") < 18) | (col("status") == "student")
        """

        other = _ensure_condition(other)

        combined_plan = (
            (OP_OR, self.plan, other.plan)
            if self.plan is not None and other.plan is not None
            else None
        )

        return Condition(
            predicate=lambda row: self(row) or other(row),
            columns=self.columns | other.columns,
            description=f"({self.description}) OR ({other.description})",
            equality_conditions=tuple(),
            plan=combined_plan,
        )

    def __invert__(self) -> Condition:
        """
        Negates a condition using logical NOT.

        Usage:
            ~col("email").like("%@spam.com")
        """

        return Condition(
            predicate=lambda row: not self(row),
            columns=self.columns,
            description=f"NOT ({self.description})",
            equality_conditions=tuple(),
            plan=(OP_NOT, self.plan) if self.plan is not None else None,
        )

    def __bool__(self) -> bool:
        """
        Prevents accidental use of Python's 'and', 'or', 'not'.

        Python does not allow overriding the behavior of the keywords:
            and / or / not

        Therefore RelPy uses:
            & / | / ~

        This method raises an error if the user accidentally writes:

            condition1 and condition2

        instead of:

            condition1 & condition2
        """

        raise TypeError(
            "RelPy Condition objects cannot be used with Python 'and/or/not'. "
            "Use '&' for AND, '|' for OR, and '~' for NOT."
        )


def _ensure_condition(value: Any) -> Condition:
    """
    Validates that a value is a Condition.
    """

    if not isinstance(value, Condition):
        raise TypeError("Expected a RelPy Condition.")

    return value

def _merge_equality_conditions(
        left: tuple[tuple[str, Any], ...],
        right: tuple[tuple[str, Any], ...],
) -> tuple[tuple[str, Any], ...]:
    """
    Merges equality metadata for AND conditions.

    If the same column has conflicting values, returns an empty tuple.
    The predicate will still filter correctly, but no index optimization is used.
    """

    merged: dict[str, Any] = {}

    for column_name, value in left + right:
        if column_name in merged and merged[column_name] != value:
            return tuple()

        merged[column_name] = value

    return tuple(merged.items())



# =============================================================================
# Column references
# =============================================================================

class ColumnRef:
    """
    Represents a reference to a column inside a condition.

    Users should usually create ColumnRef objects using col():

        col("age")
        col("email")
    """

    def __init__(self, column_name: str):
        if not isinstance(column_name, str) or not column_name.strip():
            raise ValueError("column_name must be a non-empty string.")

        self.column_name = column_name

    def _value(self, row: dict[str, Any]) -> Any:
        """
        Reads this column's value from a row.
        """

        if self.column_name not in row:
            raise KeyError(f"Column '{self.column_name}' does not exist in row.")

        return row[self.column_name]

    def _condition(
            self,
            predicate: Callable[[Any], bool],
            description: str,
            plan: tuple | None = None,
    ) -> Condition:
        """
        Creates a Condition based on this column.
        """

        return Condition(
            predicate=lambda row: predicate(self._value(row)),
            columns=frozenset({self.column_name}),
            description=description,
            plan=plan,
        )

    # -------------------------------------------------------------------------
    # Comparison operators
    # -------------------------------------------------------------------------

    def __eq__(self, other: Any) -> Condition:  # type: ignore[override]
        return Condition(
            predicate=lambda row: self._value(row) == other,
            columns=frozenset({self.column_name}),
            description=f"{self.column_name} == {other!r}",
            equality_conditions=((self.column_name, other),),
            plan=(OP_EQ, self.column_name, other),
        )

    def __ne__(self, other: Any) -> Condition:  # type: ignore[override]
        return self._condition(
            lambda value: value != other,
            f"{self.column_name} != {other!r}",
            plan=(OP_NE, self.column_name, other),
        )

    def __gt__(self, other: Any) -> Condition:
        return self._condition(
            lambda value: False if value is None else value > other,
            f"{self.column_name} > {other!r}",
            plan=(OP_GT, self.column_name, other),
        )

    def __ge__(self, other: Any) -> Condition:
        return self._condition(
            lambda value: False if value is None else value >= other,
            f"{self.column_name} >= {other!r}",
            plan=(OP_GE, self.column_name, other),
        )

    def __lt__(self, other: Any) -> Condition:
        return self._condition(
            lambda value: False if value is None else value < other,
            f"{self.column_name} < {other!r}",
            plan=(OP_LT, self.column_name, other),
        )

    def __le__(self, other: Any) -> Condition:
        return self._condition(
            lambda value: False if value is None else value <= other,
            f"{self.column_name} <= {other!r}",
            plan=(OP_LE, self.column_name, other),
        )

    # -------------------------------------------------------------------------
    # SQL-like operators
    # -------------------------------------------------------------------------

    def like(
            self,
            pattern: str,
            case_sensitive: bool = True,
    ) -> Condition:
        """
        SQL-like pattern matching.

        Supported wildcards:
            % = any sequence of characters
            _ = exactly one character

        Examples:
            col("name").like("A%")
            col("email").like("%@gmail.com")
            col("code").like("A_3")
        """

        if not isinstance(pattern, str):
            raise TypeError("LIKE pattern must be a string.")

        if type(case_sensitive) is not bool:
            raise TypeError("case_sensitive must be a bool.")

        regex = self._like_pattern_to_regex(pattern)
        flags = 0 if case_sensitive else re.IGNORECASE
        compiled = re.compile(regex, flags=flags)

        return self._condition(
            lambda value: False if value is None or not isinstance(value, str)
            else compiled.match(value) is not None,
            f"{self.column_name} LIKE {pattern!r}",
        )

    def in_(self, values: list[Any] | tuple[Any, ...] | set[Any]) -> Condition:
        """
        SQL-like IN.

        Because 'in' is a reserved word in Python, RelPy uses in_().

        Example:
            col("status").in_(["paid", "pending"])
        """

        if not isinstance(values, (list, tuple, set)):
            raise TypeError("in_() expects a list, tuple, or set.")

        value_set = set(values)

        return self._condition(
            lambda value: value in value_set,
            f"{self.column_name} IN {list(value_set)!r}",
            plan=(OP_IN, self.column_name, value_set),
        )

    def between(
            self,
            low: Any,
            high: Any,
            inclusive: bool = True,
    ) -> Condition:
        """
        SQL-like BETWEEN.

        By default this is inclusive, like SQL:

            low <= value <= high

        Example:
            col("amount").between(100, 500)
        """

        if type(inclusive) is not bool:
            raise TypeError("inclusive must be a bool.")

        if inclusive:
            return self._condition(
                lambda value: False if value is None else low <= value <= high,
                f"{self.column_name} BETWEEN {low!r} AND {high!r}",
                plan=(OP_BETWEEN, self.column_name, low, high, True),
            )

        return self._condition(
            lambda value: False if value is None else low < value < high,
            f"{self.column_name} BETWEEN {low!r} AND {high!r} EXCLUSIVE",
            plan=(OP_BETWEEN, self.column_name, low, high, False),
        )

    def is_null(self) -> Condition:
        """
        Checks whether the column value is None.
        """

        return self._condition(
            lambda value: value is None,
            f"{self.column_name} IS NULL",
            plan=(OP_ISNULL, self.column_name),
        )

    def is_not_null(self) -> Condition:
        """
        Checks whether the column value is not None.
        """

        return self._condition(
            lambda value: value is not None,
            f"{self.column_name} IS NOT NULL",
            plan=(OP_NOTNULL, self.column_name),
        )

    def _like_pattern_to_regex(self, pattern: str) -> str:
        """
        Converts SQL LIKE pattern into a regular expression.
        """

        regex_parts: list[str] = ["^"]

        for char in pattern:
            if char == "%":
                regex_parts.append(".*")
            elif char == "_":
                regex_parts.append(".")
            else:
                regex_parts.append(re.escape(char))

        regex_parts.append("$")

        return "".join(regex_parts)


import datetime as _dt

# Types whose instances are immutable and therefore safe to share between the
# caller's data and a Query's isolated copy without a deep copy.
_IMMUTABLE_SCALARS = (
    int, float, bool, str, bytes, type(None),
    _dt.datetime, _dt.date, _dt.time,
    frozenset, tuple,
)


def _isolate_row(row: dict[str, Any]) -> dict[str, Any]:
    """
    Returns an independent copy of a row for a Query's private storage.

    Equivalent in effect to ``copy.deepcopy(row)`` for RelPy's supported value
    types, but far cheaper: immutable scalar values (the overwhelming majority)
    are shared instead of copied, and only genuinely mutable values (dict, list,
    set, or unknown objects) are deep-copied. This removes the deep-copy cost
    that dominated join result construction.
    """

    out: dict[str, Any] = {}
    for key, value in row.items():
        if type(value) in _IMMUTABLE_SCALARS:
            out[key] = value
        else:
            out[key] = copy.deepcopy(value)
    return out


def col(column_name: str) -> ColumnRef:
    """
    Creates a column reference for query conditions.

    Example:
        col("age") >= 18
    """

    return ColumnRef(column_name)


def AND(*conditions: Condition) -> Condition:
    """
    Combines multiple conditions with AND.

    Alternative to using '&'.
    """

    if not conditions:
        raise ValueError("AND() requires at least one condition.")

    result = _ensure_condition(conditions[0])

    for condition in conditions[1:]:
        result = result & _ensure_condition(condition)

    return result


def OR(*conditions: Condition) -> Condition:
    """
    Combines multiple conditions with OR.

    Alternative to using '|'.
    """

    if not conditions:
        raise ValueError("OR() requires at least one condition.")

    result = _ensure_condition(conditions[0])

    for condition in conditions[1:]:
        result = result | _ensure_condition(condition)

    return result


def NOT(condition: Condition) -> Condition:
    """
    Negates a condition.

    Alternative to using '~'.
    """

    return ~_ensure_condition(condition)


# =============================================================================
# Query
# =============================================================================

@dataclass(frozen=True)
class OrderSpec:
    """
    Represents one ORDER BY column.
    """

    column_name: str
    descending: bool = False


@dataclass(frozen=True)
class Query:
    """
    Represents a query over one RelPy relation.

    A relation can be:
        - a real table
        - calculated rows from a view
    """

    db: Any

    table_name: str | None = None
    relation_name: str | None = None

    source_rows: tuple[dict[str, Any], ...] | None = None
    source_columns: tuple[str, ...] | None = None

    conditions: tuple[Condition | Callable[[dict[str, Any]], bool], ...] = field(default_factory=tuple)
    selected_columns: tuple[str, ...] | None = None
    order_specs: tuple[OrderSpec, ...] = field(default_factory=tuple)

    limit_count: int | None = None
    offset_count: int = 0

    distinct_enabled: bool = False

    # When set, this Query is a lazily-materialized native inner join: rows are
    # produced by the C engine on demand (see _load_source_rows), and a GROUP BY
    # applied directly on it is fused into a single C pass (see grouping.py).
    core_join_spec: Any = None

    @classmethod
    def from_rows(
            cls,
            db: Any,
            relation_name: str,
            rows: list[dict[str, Any]],
            columns: list[str] | tuple[str, ...],
    ) -> "Query":
        """
        Creates a Query from already-calculated rows.

        Used mainly for views.
        """

        if not isinstance(relation_name, str) or not relation_name.strip():
            raise ValueError("relation_name must be a non-empty string.")

        if not isinstance(columns, (list, tuple)):
            raise TypeError("columns must be a list or tuple of column names.")

        normalized_columns = list(columns)

        for column_name in normalized_columns:
            if not isinstance(column_name, str):
                raise TypeError("source column names must be strings.")

        if len(normalized_columns) != len(set(normalized_columns)):
            raise ValueError("source columns cannot contain duplicates.")

        column_set = set(normalized_columns)

        normalized_rows: list[dict[str, Any]] = []

        for row in rows:
            if not isinstance(row, dict):
                raise TypeError("source rows must be dictionaries.")

            if set(row.keys()) != column_set:
                raise ValueError(
                    "All source rows must contain exactly the declared source columns."
                )

            normalized_rows.append(_isolate_row(row))

        return cls(
            db=db,
            table_name=None,
            relation_name=relation_name,
            source_rows=tuple(normalized_rows),
            source_columns=tuple(normalized_columns),
        )
    # -------------------------------------------------------------------------
    # Query building methods
    # -------------------------------------------------------------------------

    def where(
            self,
            condition: Condition | Callable[[dict[str, Any]], bool],
    ) -> Query:
        """
        Adds a WHERE condition.

        Supports:
            - RelPy Condition objects
            - Python callables/lambdas

        Examples:
            .where(col("age") >= 18)

            .where(lambda row: row["age"] >= 18)
        """

        if isinstance(condition, Condition):
            self._validate_condition_columns(condition)
        elif not callable(condition):
            raise TypeError("where() expects a Condition or a callable.")

        return replace(
            self,
            conditions=self.conditions + (condition,),
        )

    def select(self, *columns: str | list[str] | tuple[str, ...]) -> Query:
        """
        Selects columns to include in the final result.

        Examples:
            .select("id", "name")
            .select(["id", "name"])

        If select() is not used, all columns are returned.
        """

        normalized_columns = self._normalize_select_columns(columns)

        for column_name in normalized_columns:
            self._validate_column_exists(column_name)

        return replace(
            self,
            selected_columns=tuple(normalized_columns),
        )

    def order_by(
            self,
            *columns: str | tuple[str, str],
            descending: bool = False,
    ) -> Query:
        """
        Adds ORDER BY behavior.

        Examples:
            .order_by("name")
            .order_by("age", descending=True)
            .order_by("status", "age")
            .order_by(("status", "asc"), ("age", "desc"))
        """

        if type(descending) is not bool:
            raise TypeError("descending must be a bool.")

        if not columns:
            raise ValueError("order_by() requires at least one column.")

        specs: list[OrderSpec] = []

        for item in columns:
            if isinstance(item, str):
                column_name = item
                is_descending = descending

            elif isinstance(item, tuple) and len(item) == 2:
                column_name, direction = item

                if not isinstance(column_name, str):
                    raise TypeError("order_by column name must be a string.")

                if not isinstance(direction, str):
                    raise TypeError("order_by direction must be a string.")

                normalized_direction = direction.strip().lower()

                if normalized_direction not in {"asc", "desc"}:
                    raise ValueError("order_by direction must be 'asc' or 'desc'.")

                is_descending = normalized_direction == "desc"

            else:
                raise TypeError(
                    "order_by() expects column names or (column, direction) tuples."
                )

            self._validate_column_exists(column_name)
            specs.append(OrderSpec(column_name, is_descending))

        return replace(
            self,
            order_specs=self.order_specs + tuple(specs),
        )

    # Alias for users who prefer orderby.
    def orderby(
            self,
            *columns: str | tuple[str, str],
            descending: bool = False,
    ) -> Query:
        """
        Alias for order_by().
        """

        return self.order_by(*columns, descending=descending)

    def group_by(self, *columns: str | list[str] | tuple[str, ...]):
        """
        Starts a grouped query.

        Example:
            db.query("orders")
              .group_by("status")
              .aggregate(total_amount=sum_("amount"))
        """

        normalized_columns = self._normalize_group_by_columns(columns)

        for column_name in normalized_columns:
            self._validate_column_exists(column_name)

        from .grouping import GroupedQuery

        return GroupedQuery(
            source_query=self,
            group_columns=tuple(normalized_columns),
        )

    def _normalize_group_by_columns(
            self,
            columns: tuple[str | list[str] | tuple[str, ...], ...],
    ) -> list[str]:
        """
        Normalizes group_by() arguments.

        Supports:
            group_by("status")
            group_by("user_id", "status")
            group_by(["user_id", "status"])
        """

        if not columns:
            raise QueryError("group_by() requires at least one column.")

        if len(columns) == 1 and isinstance(columns[0], (list, tuple)):
            normalized = list(columns[0])
        else:
            normalized = list(columns)

        for column_name in normalized:
            if not isinstance(column_name, str):
                raise QueryTypeError("group_by() column names must be strings.")

        if len(normalized) != len(set(normalized)):
            raise QueryError("group_by() cannot contain duplicate columns.")

        return normalized


    def limit(self, count: int | None) -> "Query":
        """
        Limits the number of rows returned by the query.

        Examples:
            db.query("users").limit(10).to_list()

            db.query("users")
              .order_by("id")
              .offset(20)
              .limit(10)
              .to_list()

        Passing None removes the limit.
        """

        if count is None:
            return replace(
                self,
                limit_count=None,
            )

        self._validate_non_negative_int(count, "limit")

        return replace(
            self,
            limit_count=count,
        )

    def offset(self, count: int) -> "Query":
        """
        Skips the first N rows of the query result.

        Example:
            db.query("users")
              .order_by("id")
              .offset(20)
              .limit(10)
              .to_list()
        """

        self._validate_non_negative_int(count, "offset")

        return replace(
            self,
            offset_count=count,
        )

    def distinct(self, enabled: bool = True) -> "Query":
        """
        Enables or disables DISTINCT behavior.

        DISTINCT removes duplicate rows from the final query result.

        Examples:
            db.query("orders") \
              .select("status") \
              .distinct() \
              .to_list()

            db.query("orders") \
              .distinct(False) \
              .to_list()
        """

        if type(enabled) is not bool:
            raise QueryTypeError("distinct enabled value must be a bool.")

        return replace(
            self,
            distinct_enabled=enabled,
        )

    # -------------------------------------------------------------------------
    # Terminal result methods
    # -------------------------------------------------------------------------

    def to_list(self, *, decrypt: bool = False) -> list[dict[str, Any]]:
        """
        Executes the query and returns a list of public dictionaries.

        Encrypted values are masked by default. Pass decrypt=True to decrypt them.
        """

        rows = self._execute_rows(project=True, apply_order=True)

        return [
            self.db._stored_row_to_export_dict(
                row,
                table_name=self.table_name,
                decrypt=decrypt,
            )
            for row in rows
        ]

    def to_json(
        self,
        indent: int | None = 2,
        ensure_ascii: bool = False,
        *,
        decrypt: bool = False,
    ) -> str:
        """
        Executes the query and returns the result as JSON.
        """

        return json.dumps(
            self.to_list(decrypt=decrypt),
            indent=indent,
            ensure_ascii=ensure_ascii,
            default=self._json_default,
        )

    def to_pandas(self, *, decrypt: bool = False):
        """
        Executes the query and returns the result as pandas.DataFrame.
        """

        from ._bootstrap import ensure
        pd = ensure("pandas")

        rows = self.to_list(decrypt=decrypt)
        columns = list(rows[0].keys()) if rows else self._result_columns()

        return pd.DataFrame(rows, columns=columns)

    def to_numpy(
        self,
        dtype: Any | None = None,
        include_columns: bool = False,
        *,
        decrypt: bool = False,
    ):
        """
        Executes the query and returns the result as NumPy array.
        """

        from ._bootstrap import ensure
        np = ensure("numpy")

        rows = self.to_list(decrypt=decrypt)
        columns = list(rows[0].keys()) if rows else self._result_columns()

        matrix = [
            [row.get(column_name) for column_name in columns]
            for row in rows
        ]

        array_dtype = dtype if dtype is not None else object
        array = np.array(matrix, dtype=array_dtype)

        if include_columns:
            return array, columns

        return array

    def __iter__(self):
        """
        Allows direct iteration over query results.

        Example:
            for row in db.query("users").where(col("age") >= 18):
                print(row)
        """

        return iter(self.to_list())

    def first(self, *, decrypt: bool = False) -> dict[str, Any] | None:
        """
        Executes the query and returns the first public row.
        """

        rows = self.to_list(decrypt=decrypt)

        if not rows:
            return None

        return rows[0]

    def one(self, *, decrypt: bool = False) -> dict[str, Any]:
        """
        Executes the query and expects exactly one public row.
        """

        rows = self.to_list(decrypt=decrypt)

        if len(rows) == 0:
            raise NoRowsFoundError(
                f"Expected exactly one row from relation "
                f"'{self._relation_display_name()}', but found 0."
            )

        if len(rows) > 1:
            raise MultipleRowsFoundError(
                f"Expected exactly one row from relation "
                f"'{self._relation_display_name()}', but found {len(rows)}."
            )

        return rows[0]


    def exists(self) -> bool:
        """
        Executes the query and returns True if at least one row exists.

        Returns False otherwise.

        Respects:
            - where
            - offset
            - limit
        """

        rows = self._execute_rows(
            project=False,
            apply_order=True,
        )

        return len(rows) > 0

    def pluck(self, column_name: str, *, decrypt: bool = False) -> list[Any]:
        """
        Executes the query and returns a list of values from one column.
        """

        if not isinstance(column_name, str) or not column_name.strip():
            raise ValueError("pluck() column_name must be a non-empty string.")

        self._validate_column_exists(column_name)

        query = replace(
            self,
            selected_columns=(column_name,),
        )

        rows = query.to_list(decrypt=decrypt)

        return [
            row[column_name]
            for row in rows
        ]

    # -------------------------------------------------------------------------
    # Aggregations
    # -------------------------------------------------------------------------

    def _core_plan(self):
        """If this is a plain single-table query with a fully structured WHERE,
        returns (c_engine, table_name, plan) for native execution; else None."""
        if (self.table_name is None or self.source_rows is not None
                or self.core_join_spec is not None):
            return None
        mirror = self.db._core_mirror_for(self.table_name)
        if mirror is None:
            return None
        plan = None
        for condition in self.conditions:
            if not (isinstance(condition, Condition) and condition.plan is not None):
                return None
            plan = condition.plan if plan is None else (OP_AND, plan, condition.plan)
        engine, _cols = mirror
        return engine, self.table_name, plan

    def _core_simple(self) -> bool:
        """Whether row-level modifiers that the aggregate fast path can't honor
        are absent (so a WHERE-only native aggregate is exact)."""
        return (not self.distinct_enabled
                and self.offset_count == 0
                and self.limit_count is None)

    def count(self, column_name: str | None = None) -> int:
        """
        Counts rows.

        If column_name is None:
            counts rows after WHERE, DISTINCT, OFFSET, and LIMIT.

        If column_name is provided:
            counts non-null values in that column.

        With distinct enabled:
            count("status") counts distinct non-null status values.
        """

        if column_name is None:
            cp = self._core_plan()
            if cp is not None and self._core_simple():
                engine, table, plan = cp
                return engine.filter_count(table, plan)

            rows = self._execute_rows(
                project=self.selected_columns is not None,
                apply_order=True,
            )

            return len(rows)

        cp = self._core_plan()
        if (cp is not None and self._core_simple()
                and column_name in self.db.schema[self.table_name].columns):
            engine, table, plan = cp
            return engine.aggregate(table, plan, [("v", "count", column_name)])["v"]

        values = self.pluck(column_name)

        return builtins.sum(
            1
            for value in values
            if value is not None
        )


    def sum(self, column_name: str) -> int | float | None:
        """
        Returns the sum of a numeric column after WHERE.

        None values are ignored.

        Returns None if there are no non-null values.
        """

        cp = self._core_plan()
        if cp is not None and self.db._core_numeric_column(self.table_name, column_name):
            engine, table, plan = cp
            res = engine.aggregate(table, plan,
                                   [("v", "sum", column_name), ("c", "count", column_name)])
            return None if res["c"] == 0 else res["v"]

        values = self._numeric_values(column_name)

        if not values:
            return None

        return builtins.sum(values)

    def average(self, column_name: str) -> float | None:
        """
        Returns the average of a numeric column after WHERE.

        None values are ignored.

        Returns None if there are no non-null values.
        """

        cp = self._core_plan()
        if cp is not None and self.db._core_numeric_column(self.table_name, column_name):
            engine, table, plan = cp
            return engine.aggregate(table, plan, [("v", "avg", column_name)])["v"]

        values = self._numeric_values(column_name)

        if not values:
            return None

        return builtins.sum(values) / len(values)

    def avg(self, column_name: str) -> float | None:
        """
        Alias for average().
        """

        return self.average(column_name)

    def min(self, column_name: str) -> Any:
        """
        Returns the minimum value of a column after WHERE.

        None values are ignored.

        Returns None if there are no non-null values.
        """

        cp = self._core_plan()
        if cp is not None and self.db._core_numeric_column(self.table_name, column_name):
            engine, table, plan = cp
            return engine.aggregate(table, plan, [("v", "min", column_name)])["v"]

        values = self._non_null_values(column_name)

        if not values:
            return None

        return builtins.min(values)

    def max(self, column_name: str) -> Any:
        """
        Returns the maximum value of a column after WHERE.

        None values are ignored.

        Returns None if there are no non-null values.
        """

        cp = self._core_plan()
        if cp is not None and self.db._core_numeric_column(self.table_name, column_name):
            engine, table, plan = cp
            return engine.aggregate(table, plan, [("v", "max", column_name)])["v"]

        values = self._non_null_values(column_name)

        if not values:
            return None

        return builtins.max(values)


    # -------------------------------------------------------------------------
    # JOIN
    # -------------------------------------------------------------------------

    def join(
            self,
            other: Any,
            *,
            on: str | tuple[str, str] | list[tuple[str, str]] | tuple[tuple[str, str], ...] | None = None,
            left_on: str | list[str] | tuple[str, ...] | None = None,
            right_on: str | list[str] | tuple[str, ...] | None = None,
            method: str = "inner",
            left_prefix: str | None = None,
            right_prefix: str | None = None,
            prefix_columns: bool = True,
            preserve_prefixed_columns: bool = True,
            suffixes: tuple[str, str] = ("_left", "_right"),
            match_nulls: bool = False,
    ) -> "Query":
        """
        Joins this query with another table, view, or query.

        Supported join methods:
            regular
            inner
            natural
            left
            right
            full
            cross

        Examples:
            db.query("orders").join("users")

            db.query("orders").join(
                "users",
                on=("user_id", "id"),
                method="inner",
            )

            db.query("users").join(
                "orders",
                on=("id", "user_id"),
                method="left",
            )

            db.query("orders").join(
                "users",
                method="natural",
            )
        """

        from .joins import join_queries

        return join_queries(
            left_query=self,
            other=other,
            on=on,
            left_on=left_on,
            right_on=right_on,
            method=method,
            left_prefix=left_prefix,
            right_prefix=right_prefix,
            prefix_columns=prefix_columns,
            preserve_prefixed_columns=preserve_prefixed_columns,
            suffixes=suffixes,
            match_nulls=match_nulls,
        )

    def inner_join(self, other: Any, **kwargs) -> "Query":
        """
        Shortcut for join(..., method="inner").
        """

        return self.join(other, method="inner", **kwargs)

    def left_join(self, other: Any, **kwargs) -> "Query":
        """
        Shortcut for join(..., method="left").
        """

        return self.join(other, method="left", **kwargs)

    def right_join(self, other: Any, **kwargs) -> "Query":
        """
        Shortcut for join(..., method="right").
        """

        return self.join(other, method="right", **kwargs)

    def full_join(self, other: Any, **kwargs) -> "Query":
        """
        Shortcut for join(..., method="full").
        """

        return self.join(other, method="full", **kwargs)

    def cross_join(self, other: Any, **kwargs) -> "Query":
        """
        Shortcut for join(..., method="cross").
        """

        return self.join(other, method="cross", **kwargs)

    def natural_join(self, other: Any, **kwargs) -> "Query":
        """
        Shortcut for join(..., method="natural").

        NATURAL JOIN joins by columns with the same names.
        It does not use foreign keys.
        """

        return self.join(other, method="natural", **kwargs)

    # -------------------------------------------------------------------------
    # Internal execution
    # -------------------------------------------------------------------------

    def _execute_rows(
            self,
            project: bool,
            apply_order: bool,
    ) -> list[dict[str, Any]]:
        """
        Executes the query pipeline.

        Internal execution order:
            1. source relation rows
            2. where
            3. order_by
            4. select/projection
            5. distinct
            6. offset
            7. limit
        """

        rows = self._load_source_rows()

        rows = self._apply_conditions(rows)

        if apply_order:
            rows = self._apply_order_by(rows)

        if project:
            rows = self._apply_select(rows)

        if self.distinct_enabled:
            rows = self._apply_distinct(rows)

        rows = self._apply_offset_limit(rows)

        return rows


    def _execute_rows_for_grouping(self) -> list[dict[str, Any]]:
        """
        Returns rows for GROUP BY.

        This applies WHERE conditions, but does not apply:
            - select
            - order_by
            - distinct
            - offset
            - limit

        GROUP BY creates a new relation shape, so these operations should
        be applied on the GroupedQuery instead.
        """

        rows = self._load_source_rows()
        rows = self._apply_conditions(rows)

        return rows

    def _decorate_with_query_rows(
        self,
        rows: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """
        Pairs each stored row with its query-execution view.

        The query-execution view is what WHERE conditions and ORDER BY see:
        encrypted payloads are decrypted automatically when an encryption key
        is available, otherwise the row is returned unchanged.

        This is computed once per row so that _apply_conditions() and
        _apply_order_by() do not repeatedly redo the same decryption check for
        every condition or every ORDER BY column.

        Returns:
            A list of (query_row, row) pairs, in the same order as `rows`.
        """

        if not self.db._query_view_needs_processing():
            # No encrypted columns exist, so the stored row is already a valid
            # query view. Avoid the per-row encryption scan and transform.
            return [(row, row) for row in rows]

        return [
            (self.db._row_for_query_execution(row), row)
            for row in rows
        ]

    def _apply_conditions(
        self,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Applies all WHERE conditions.

        Conditions receive query execution rows. Encrypted payloads are decrypted
        automatically when an encryption key is available.
        """

        if not self.conditions:
            return rows

        # Build the query view (decrypted where necessary). When no encrypted
        # columns exist, the query view IS the stored row, so we avoid copying.
        needs_view = self.db._query_view_needs_processing()
        if needs_view:
            query_rows = [self.db._row_for_query_execution(r) for r in rows]
        else:
            query_rows = rows

        # Evaluate every structurally-planned condition in a single C pass,
        # then apply any remaining (unplanned) conditions — e.g. raw lambdas or
        # LIKE regexes, which cannot be expressed as a plan — in Python.
        remaining = self.conditions
        planned = [
            c.plan
            for c in self.conditions
            if isinstance(c, Condition) and c.plan is not None
        ]
        if planned:
            combined = planned[0]
            for p in planned[1:]:
                combined = (OP_AND, combined, p)
            try:
                keep_indices = _c_engine.filter_indices(query_rows, combined)
            except Exception:
                # An unplannable value (e.g. an unhashable IN member) — evaluate
                # this predicate with the Python predicate instead.
                keep_indices = None
            if keep_indices is not None:
                new_rows = [rows[i] for i in keep_indices]
                query_rows = (
                    new_rows if not needs_view
                    else [query_rows[i] for i in keep_indices]
                )
                rows = new_rows
                remaining = tuple(
                    c
                    for c in self.conditions
                    if not (isinstance(c, Condition) and c.plan is not None)
                )

        # Any conditions the C pass did not handle (raw callables, LIKE
        # regexes) run in Python.
        for condition in remaining:
            kept_rows: list[dict[str, Any]] = []
            kept_query_rows: list[dict[str, Any]] = []

            for query_row, row in zip(query_rows, rows):
                if isinstance(condition, Condition):
                    keep = condition(query_row)
                else:
                    keep = condition(dict(query_row))

                    if type(keep) is not bool:
                        raise TypeError(
                            "where() callable must return bool for every row."
                        )

                if keep:
                    kept_rows.append(row)
                    kept_query_rows.append(query_row)

            rows = kept_rows
            query_rows = kept_query_rows

        return rows

    def _apply_order_by(
        self,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Applies ORDER BY rules.
        """

        if not self.order_specs:
            return rows

        for spec in self.order_specs:
            self._validate_column_exists(spec.column_name)

        # Decorate once: every order_spec reuses the same decrypted view
        # instead of recomputing it (up to 3x per spec, per row).
        decorated = self._decorate_with_query_rows(rows)

        for spec in reversed(self.order_specs):
            def sort_key(pair: tuple[dict[str, Any], dict[str, Any]]) -> Any:
                query_row, _ = pair
                return query_row[spec.column_name]

            non_null_pairs = [
                pair
                for pair in decorated
                if sort_key(pair) is not None
            ]

            null_pairs = [
                pair
                for pair in decorated
                if sort_key(pair) is None
            ]

            non_null_pairs.sort(
                key=sort_key,
                reverse=spec.descending,
            )

            decorated = non_null_pairs + null_pairs

        return [row for _, row in decorated]

    def _apply_select(
            self,
            rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Applies SELECT projection.

        If no selected columns were provided, all columns are returned.
        """

        if self.selected_columns is None:
            return rows

        return [
            {
                column_name: row[column_name]
                for column_name in self.selected_columns
            }
            for row in rows
        ]

    def _apply_offset_limit(
            self,
            rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Applies OFFSET and LIMIT.

        OFFSET is applied before LIMIT.
        """

        start = self.offset_count

        if self.limit_count is None:
            return rows[start:]

        end = start + self.limit_count

        return rows[start:end]

    def _apply_distinct(
            self,
            rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Removes duplicate rows.

        Works also with values that are normally unhashable,
        such as dicts, lists, and sets.
        """

        seen: set[Any] = set()
        unique_rows: list[dict[str, Any]] = []

        for row in rows:
            row_key = self._row_distinct_key(row)

            if row_key in seen:
                continue

            seen.add(row_key)
            unique_rows.append(row)

        return unique_rows

    def _row_distinct_key(
            self,
            row: dict[str, Any],
    ) -> tuple[tuple[str, Any], ...]:
        """
        Creates a hashable key for a row.

        The key includes column names so that row structure matters.
        """

        return tuple(
            (column_name, make_hashable(row[column_name]))
            for column_name in row.keys()
        )

    # -------------------------------------------------------------------------
    # Internal aggregation helpers
    # -------------------------------------------------------------------------

    def _non_null_values(self, column_name: str) -> list[Any]:
        """
        Returns non-null values for a column after the query pipeline.

        This respects:
            - where
            - order_by
            - distinct
            - offset
            - limit
        """

        values = self.pluck(column_name)

        return [
            value
            for value in values
            if value is not None
        ]


    def _numeric_values(self, column_name: str) -> list[int | float]:
        """
        Returns numeric non-null values for a column after WHERE.

        bool is not treated as a number here.
        """

        values = self._non_null_values(column_name)

        numeric_values: list[int | float] = []

        for value in values:
            if type(value) is bool or not isinstance(value, (int, float)):
                raise TypeError(
                    f"Column '{self.table_name}.{column_name}' contains "
                    f"non-numeric value {value!r} of type {type(value).__name__}."
                )

            numeric_values.append(value)

        return numeric_values

    # -------------------------------------------------------------------------
    # Internal validation helpers
    # -------------------------------------------------------------------------

    def _validate_table_exists(self) -> None:
        """
        Validates that the query table exists.

        If this Query is based on source_rows, it is not querying a real table.
        """

        if self.source_rows is not None or self.core_join_spec is not None:
            return

        if self.table_name is None:
            raise RuntimeError("Query has no table_name and no source_rows.")

        self.db._validate_existing_table(self.table_name)


    def _validate_column_exists(self, column_name: str) -> None:
        """
        Validates that a column exists in the query relation.
        """

        if column_name not in self._available_columns():
            raise ColumnNotFoundError(
                f"Column '{column_name}' does not exist in relation "
                f"'{self._relation_display_name()}'."
            )


    def _validate_condition_columns(self, condition: Condition) -> None:
        """
        Validates that all columns used by a Condition exist.
        """

        for column_name in condition.columns:
            self._validate_column_exists(column_name)

    def _normalize_select_columns(
            self,
            columns: tuple[str | list[str] | tuple[str, ...], ...],
    ) -> list[str]:
        """
        Normalizes select() arguments.

        Supports:
            select("id", "name")
            select(["id", "name"])
        """

        if not columns:
            raise ValueError("select() requires at least one column.")

        if len(columns) == 1 and isinstance(columns[0], (list, tuple)):
            normalized = list(columns[0])
        else:
            normalized = list(columns)

        for column_name in normalized:
            if not isinstance(column_name, str):
                raise TypeError("select() column names must be strings.")

        if len(normalized) != len(set(normalized)):
            raise ValueError("select() cannot contain duplicate columns.")

        return normalized

    def _result_columns(self) -> list[str]:
        """
        Returns the columns that appear in the final query result.
        """

        if self.selected_columns is not None:
            return list(self.selected_columns)

        return self._available_columns()

    def _validate_non_negative_int(
            self,
            value: int,
            name: str,
    ) -> None:
        """
        Validates that a value is a non-negative integer.

        bool is rejected, even though bool is technically a subclass of int in Python.
        """

        if type(value) is bool or not isinstance(value, int):
            raise QueryTypeError(f"{name} must be a non-negative integer.")

        if value < 0:
            raise QueryError(f"{name} cannot be negative.")

    # -------------------------------------------------------------------------
    # JSON helper
    # -------------------------------------------------------------------------

    def _json_default(self, value: Any) -> Any:
        """
        JSON serializer fallback.

        Uses db._json_default if it exists.
        Otherwise falls back to the shared default JSON encoder, which
        supports bytes and datetime/date/time values.
        """

        if hasattr(self.db, "_json_default"):
            return self.db._json_default(value)

        return default_json_encoder(value)

    # -------------------------------------------------------------------------
    # View helper
    # -------------------------------------------------------------------------

    def _relation_display_name(self) -> str:
        """
        Returns the user-facing relation name.
        """

        return self.relation_name or self.table_name or "<unknown>"

    def _available_columns(self) -> list[str]:
        """
        Returns the columns available in this Query's source relation.
        """

        if self.source_columns is not None:
            return list(self.source_columns)

        if self.source_rows is not None:
            if not self.source_rows:
                return []

            return list(self.source_rows[0].keys())

        if self.table_name is None:
            raise RuntimeError("Query has no table_name and no source columns.")

        return list(self.db.schema[self.table_name].columns.keys())


    # -------------------------------------------------------------------------
    # Indexing helper
    # -------------------------------------------------------------------------

    def _load_source_rows(self) -> list[dict[str, Any]]:
        """
        Loads source rows.

        If possible, uses indexes that point to stable internal row ids.
        """

        if self.source_rows is not None:
            return [
                dict(row)
                for row in self.source_rows
            ]

        if self.core_join_spec is not None:
            return self._materialize_core_join()

        self._validate_table_exists()

        indexed_row_ids = self._try_index_lookup_row_ids()

        if indexed_row_ids is not None:
            return self.db._load_rows_by_row_ids(
                table_name=self.table_name,
                row_ids=indexed_row_ids,
            )

        return [
            self.db._stored_row_without_internal_fields(row)
            for row in self.db.data[self.table_name]
        ]

    def _materialize_core_join(self) -> list[dict[str, Any]]:
        """Produces the joined rows for a lazy native inner join, in C."""
        spec = self.core_join_spec
        engine = self.db._core_join_engine(spec["left_table"], spec["right_table"])
        if engine is None:
            raise RuntimeError("native join engine unavailable")
        return engine.hash_join(
            spec["left_table"], spec["left_plan"], spec["left_keys"],
            spec["right_table"], spec["right_plan"], spec["right_keys"],
            list(spec["left_map"].items()), list(spec["right_map"].items()),
        )

    def _try_index_lookup_row_ids(self) -> set[int] | None:
        """
        Tries to find indexed row ids for simple equality conditions.
        """

        if self.table_name is None:
            return None

        if not hasattr(self.db, "_lookup_indexed_row_ids"):
            return None

        equality_criteria: dict[str, Any] = {}

        for condition in self.conditions:
            if not isinstance(condition, Condition):
                continue

            for column_name, value in condition.equality_conditions:
                if column_name not in self._available_columns():
                    continue

                if (
                        column_name in equality_criteria
                        and equality_criteria[column_name] != value
                ):
                    return set()

                equality_criteria[column_name] = value

        if not equality_criteria:
            return None

        return self.db._lookup_indexed_row_ids(
            table_name=self.table_name,
            equality_criteria=equality_criteria,
        )
