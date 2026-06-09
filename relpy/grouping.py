from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
import builtins
import json
import base64
import datetime as dt

from .exceptions import (
    ColumnNotFoundError,
    QueryError,
    QueryTypeError,
)
from .queries import Condition, OrderSpec


# =============================================================================
# Aggregation specs
# =============================================================================

@dataclass(frozen=True)
class AggregationSpec:
    """
    Represents one aggregation operation.

    Examples:
        count()
        count("email")
        sum_("amount")
        avg("amount")
        min_("amount")
        max_("amount")
    """

    function_name: str
    column_name: str | None = None


def count(column_name: str | None = None) -> AggregationSpec:
    """
    Counts rows or non-null values.

    count()
        Counts all rows in each group.

    count("email")
        Counts only rows where email is not None.
    """

    if column_name is not None and (
        not isinstance(column_name, str) or not column_name.strip()
    ):
        raise QueryTypeError("count() column_name must be a non-empty string or None.")

    return AggregationSpec(
        function_name="count",
        column_name=column_name,
    )


def sum_(column_name: str) -> AggregationSpec:
    """
    Sums a numeric column.

    Named sum_ because sum is a Python built-in.
    """

    _validate_aggregation_column_name(column_name, "sum_")

    return AggregationSpec(
        function_name="sum",
        column_name=column_name,
    )


def avg(column_name: str) -> AggregationSpec:
    """
    Calculates the average of a numeric column.
    """

    _validate_aggregation_column_name(column_name, "avg")

    return AggregationSpec(
        function_name="avg",
        column_name=column_name,
    )


def min_(column_name: str) -> AggregationSpec:
    """
    Returns the minimum non-null value of a column.

    Named min_ because min is a Python built-in.
    """

    _validate_aggregation_column_name(column_name, "min_")

    return AggregationSpec(
        function_name="min",
        column_name=column_name,
    )


def max_(column_name: str) -> AggregationSpec:
    """
    Returns the maximum non-null value of a column.

    Named max_ because max is a Python built-in.
    """

    _validate_aggregation_column_name(column_name, "max_")

    return AggregationSpec(
        function_name="max",
        column_name=column_name,
    )


def _validate_aggregation_column_name(column_name: str, function_name: str) -> None:
    """
    Validates aggregation column name.
    """

    if not isinstance(column_name, str) or not column_name.strip():
        raise QueryTypeError(
            f"{function_name}() column_name must be a non-empty string."
        )


# =============================================================================
# GroupedQuery
# =============================================================================

@dataclass(frozen=True)
class GroupedQuery:
    """
    Represents a grouped query.

    It is created from:

        db.query("orders").group_by("status")

    Then aggregations can be added:

        .aggregate(total_amount=sum_("amount"))

    And HAVING can filter aggregated rows:

        .having(col("total_amount") > 100)
    """

    source_query: Any
    group_columns: tuple[str, ...]

    aggregation_specs: tuple[tuple[str, AggregationSpec], ...] = field(default_factory=tuple)
    having_conditions: tuple[Condition, ...] = field(default_factory=tuple)
    selected_columns: tuple[str, ...] | None = None
    order_specs: tuple[OrderSpec, ...] = field(default_factory=tuple)

    limit_count: int | None = None
    offset_count: int = 0
    distinct_enabled: bool = False

    # -------------------------------------------------------------------------
    # Query builder methods
    # -------------------------------------------------------------------------

    def aggregate(self, **aggregations: AggregationSpec) -> "GroupedQuery":
        """
        Adds aggregation columns to the grouped query.

        Example:
            .aggregate(
                order_count=count(),
                total_amount=sum_("amount"),
                average_amount=avg("amount"),
            )
        """

        if not aggregations:
            raise QueryError("aggregate() requires at least one aggregation.")

        available_columns = self._source_columns()

        normalized_specs: list[tuple[str, AggregationSpec]] = list(self.aggregation_specs)

        existing_aliases = {
            alias
            for alias, _ in normalized_specs
        }

        for alias, spec in aggregations.items():
            self._validate_alias(alias)

            if alias in existing_aliases:
                raise QueryError(f"Aggregation alias '{alias}' already exists.")

            if alias in self.group_columns:
                raise QueryError(
                    f"Aggregation alias '{alias}' conflicts with a group_by column."
                )

            if not isinstance(spec, AggregationSpec):
                raise QueryTypeError(
                    f"Aggregation '{alias}' must be an AggregationSpec."
                )

            if spec.column_name is not None and spec.column_name not in available_columns:
                raise ColumnNotFoundError(
                    f"Column '{spec.column_name}' does not exist in relation "
                    f"'{self.source_query._relation_display_name()}'."
                )

            normalized_specs.append((alias, spec))
            existing_aliases.add(alias)

        return replace(
            self,
            aggregation_specs=tuple(normalized_specs),
        )

    def having(self, condition: Condition) -> "GroupedQuery":
        """
        Adds a HAVING condition.

        HAVING runs after grouping and aggregation.

        Example:
            .having(col("total_amount") > 100)
        """

        if not isinstance(condition, Condition):
            raise QueryTypeError("having() expects a RelPy Condition.")

        available_columns = self._result_columns_after_aggregation()

        for column_name in condition.columns:
            if column_name not in available_columns:
                raise ColumnNotFoundError(
                    f"Column '{column_name}' does not exist in grouped relation."
                )

        return replace(
            self,
            having_conditions=self.having_conditions + (condition,),
        )

    def select(self, *columns: str | list[str] | tuple[str, ...]) -> "GroupedQuery":
        """
        Selects columns from the grouped result.

        These columns must be group columns or aggregation aliases.

        Example:
            .select("status", "total_amount")
        """

        normalized_columns = self._normalize_select_columns(columns)
        available_columns = self._result_columns_after_aggregation()

        for column_name in normalized_columns:
            if column_name not in available_columns:
                raise ColumnNotFoundError(
                    f"Column '{column_name}' does not exist in grouped relation."
                )

        return replace(
            self,
            selected_columns=tuple(normalized_columns),
        )

    def order_by(
        self,
        *columns: str | tuple[str, str],
        descending: bool = False,
    ) -> "GroupedQuery":
        """
        Sorts grouped results.

        Example:
            .order_by("total_amount", descending=True)
            .order_by(("status", "asc"), ("total_amount", "desc"))
        """

        if type(descending) is not bool:
            raise QueryTypeError("descending must be a bool.")

        if not columns:
            raise QueryError("order_by() requires at least one column.")

        available_columns = self._result_columns_after_aggregation()
        specs: list[OrderSpec] = []

        for item in columns:
            if isinstance(item, str):
                column_name = item
                is_descending = descending

            elif isinstance(item, tuple) and len(item) == 2:
                column_name, direction = item

                if not isinstance(column_name, str):
                    raise QueryTypeError("order_by column name must be a string.")

                if not isinstance(direction, str):
                    raise QueryTypeError("order_by direction must be a string.")

                normalized_direction = direction.strip().lower()

                if normalized_direction not in {"asc", "desc"}:
                    raise QueryError("order_by direction must be 'asc' or 'desc'.")

                is_descending = normalized_direction == "desc"

            else:
                raise QueryTypeError(
                    "order_by() expects column names or (column, direction) tuples."
                )

            if column_name not in available_columns:
                raise ColumnNotFoundError(
                    f"Column '{column_name}' does not exist in grouped relation."
                )

            specs.append(OrderSpec(column_name, is_descending))

        return replace(
            self,
            order_specs=self.order_specs + tuple(specs),
        )

    def orderby(
        self,
        *columns: str | tuple[str, str],
        descending: bool = False,
    ) -> "GroupedQuery":
        """
        Alias for order_by().
        """

        return self.order_by(*columns, descending=descending)

    def limit(self, count: int | None) -> "GroupedQuery":
        """
        Limits the number of grouped rows.
        """

        if count is None:
            return replace(self, limit_count=None)

        self._validate_non_negative_int(count, "limit")

        return replace(self, limit_count=count)

    def offset(self, count: int) -> "GroupedQuery":
        """
        Skips the first N grouped rows.
        """

        self._validate_non_negative_int(count, "offset")

        return replace(self, offset_count=count)

    def distinct(self, enabled: bool = True) -> "GroupedQuery":
        """
        Enables or disables DISTINCT on grouped result rows.

        Usually not needed after group_by, but supported for consistency.
        """

        if type(enabled) is not bool:
            raise QueryTypeError("distinct enabled value must be a bool.")

        return replace(self, distinct_enabled=enabled)

    # -------------------------------------------------------------------------
    # Terminal methods
    # -------------------------------------------------------------------------

    def to_list(self) -> list[dict[str, Any]]:
        """
        Executes the grouped query and returns list of dictionaries.
        """

        return self._execute_rows()

    def to_json(
        self,
        indent: int | None = 2,
        ensure_ascii: bool = False,
    ) -> str:
        """
        Executes the grouped query and returns JSON.
        """

        return json.dumps(
            self.to_list(),
            indent=indent,
            ensure_ascii=ensure_ascii,
            default=self._json_default,
        )

    def to_pandas(self):
        """
        Executes the grouped query and returns pandas.DataFrame.
        """

        try:
            import pandas as pd
        except ImportError as exc:
            raise ImportError(
                "to_pandas() requires pandas. Install it with: pip install pandas"
            ) from exc

        rows = self.to_list()
        columns = self._result_columns()

        return pd.DataFrame(rows, columns=columns)

    def to_numpy(
        self,
        dtype: Any | None = None,
        include_columns: bool = False,
    ):
        """
        Executes the grouped query and returns NumPy array.
        """

        try:
            import numpy as np
        except ImportError as exc:
            raise ImportError(
                "to_numpy() requires NumPy. Install it with: pip install numpy"
            ) from exc

        rows = self.to_list()
        columns = self._result_columns()

        matrix = [
            [row.get(column_name) for column_name in columns]
            for row in rows
        ]

        array_dtype = dtype if dtype is not None else object
        array = np.array(matrix, dtype=array_dtype)

        if include_columns:
            return array, columns

        return array

    def pluck(self, column_name: str) -> list[Any]:
        """
        Returns values from one grouped result column.
        """

        if not isinstance(column_name, str) or not column_name.strip():
            raise QueryTypeError("pluck() column_name must be a non-empty string.")

        if column_name not in self._result_columns_after_aggregation():
            raise ColumnNotFoundError(
                f"Column '{column_name}' does not exist in grouped relation."
            )

        rows = self._execute_rows()

        return [
            row[column_name]
            for row in rows
        ]

    def first(self) -> dict[str, Any] | None:
        """
        Returns the first grouped row, or None.
        """

        rows = self._execute_rows()

        if not rows:
            return None

        return rows[0]

    def one(self) -> dict[str, Any]:
        """
        Expects exactly one grouped row.
        """

        from .exceptions import NoRowsFoundError, MultipleRowsFoundError

        rows = self._execute_rows()

        if len(rows) == 0:
            raise NoRowsFoundError(
                "Expected exactly one grouped row, but found 0."
            )

        if len(rows) > 1:
            raise MultipleRowsFoundError(
                f"Expected exactly one grouped row, but found {len(rows)}."
            )

        return rows[0]

    def exists(self) -> bool:
        """
        Returns True if at least one grouped row exists.
        """

        return len(self._execute_rows()) > 0

    def count(self) -> int:
        """
        Counts grouped result rows.
        """

        return len(self._execute_rows())

    def __iter__(self):
        """
        Allows iteration over grouped result rows.
        """

        return iter(self.to_list())

    # -------------------------------------------------------------------------
    # Execution
    # -------------------------------------------------------------------------

    def _execute_rows(self) -> list[dict[str, Any]]:
        """
        Executes the grouped query.

        Execution order:
            1. source query rows after WHERE
            2. group rows
            3. aggregate
            4. having
            5. order_by
            6. select
            7. distinct
            8. offset
            9. limit
        """

        source_rows = self.source_query._execute_rows_for_grouping()

        grouped_rows = self._build_grouped_rows(source_rows)
        grouped_rows = self._apply_having(grouped_rows)
        grouped_rows = self._apply_order_by(grouped_rows)
        grouped_rows = self._apply_select(grouped_rows)

        if self.distinct_enabled:
            grouped_rows = self._apply_distinct(grouped_rows)

        grouped_rows = self._apply_offset_limit(grouped_rows)

        return grouped_rows

    def _build_grouped_rows(
        self,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Groups source rows and calculates aggregation rows.

        Encrypted payloads are decrypted for grouping/aggregation when a key is loaded.
        """

        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}

        for row in rows:
            query_row = self.source_query.db._row_for_query_execution(row)

            group_key = tuple(
                self._make_hashable(query_row[column_name])
                for column_name in self.group_columns
            )

            groups.setdefault(group_key, []).append(query_row)

        result_rows: list[dict[str, Any]] = []

        for group_rows in groups.values():
            first_row = group_rows[0]

            result_row: dict[str, Any] = {
                column_name: first_row[column_name]
                for column_name in self.group_columns
            }

            for alias, spec in self.aggregation_specs:
                result_row[alias] = self._calculate_aggregation(
                    group_rows=group_rows,
                    spec=spec,
                )

            result_rows.append(result_row)

        return result_rows

    def _calculate_aggregation(
        self,
        group_rows: list[dict[str, Any]],
        spec: AggregationSpec,
    ) -> Any:
        """
        Calculates one aggregation for one group.
        """

        if spec.function_name == "count":
            if spec.column_name is None:
                return len(group_rows)

            return builtins.sum(
                1
                for row in group_rows
                if row[spec.column_name] is not None
            )

        if spec.column_name is None:
            raise QueryError(
                f"Aggregation '{spec.function_name}' requires a column."
            )

        values = [
            row[spec.column_name]
            for row in group_rows
            if row[spec.column_name] is not None
        ]

        if spec.function_name == "sum":
            numeric_values = self._validate_numeric_values(values, spec.column_name)

            if not numeric_values:
                return None

            return builtins.sum(numeric_values)

        if spec.function_name == "avg":
            numeric_values = self._validate_numeric_values(values, spec.column_name)

            if not numeric_values:
                return None

            return builtins.sum(numeric_values) / len(numeric_values)

        if spec.function_name == "min":
            if not values:
                return None

            return builtins.min(values)

        if spec.function_name == "max":
            if not values:
                return None

            return builtins.max(values)

        raise QueryError(f"Unsupported aggregation '{spec.function_name}'.")

    def _apply_having(
        self,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Applies HAVING conditions.
        """

        if not self.having_conditions:
            return rows

        filtered_rows = rows

        for condition in self.having_conditions:
            next_rows: list[dict[str, Any]] = []

            for row in filtered_rows:
                if condition(row):
                    next_rows.append(row)

            filtered_rows = next_rows

        return filtered_rows

    def _apply_order_by(
        self,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Applies ORDER BY to grouped rows.
        """

        if not self.order_specs:
            return rows

        sorted_rows = list(rows)

        for spec in reversed(self.order_specs):
            non_null_rows = [
                row
                for row in sorted_rows
                if row[spec.column_name] is not None
            ]

            null_rows = [
                row
                for row in sorted_rows
                if row[spec.column_name] is None
            ]

            non_null_rows.sort(
                key=lambda row: row[spec.column_name],
                reverse=spec.descending,
            )

            sorted_rows = non_null_rows + null_rows

        return sorted_rows

    def _apply_select(
        self,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Applies select projection to grouped rows.
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
        Applies OFFSET and LIMIT to grouped rows.
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
        Removes duplicate grouped rows.
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

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _source_columns(self) -> list[str]:
        """
        Returns columns available from the source query before grouping.
        """

        return self.source_query._available_columns()

    def _result_columns_after_aggregation(self) -> list[str]:
        """
        Returns columns available after grouping and aggregation.
        """

        columns = list(self.group_columns)

        for alias, _ in self.aggregation_specs:
            columns.append(alias)

        return columns

    def _result_columns(self) -> list[str]:
        """
        Returns final output columns.
        """

        if self.selected_columns is not None:
            return list(self.selected_columns)

        return self._result_columns_after_aggregation()

    def _validate_alias(self, alias: str) -> None:
        """
        Validates aggregation alias.
        """

        if not isinstance(alias, str) or not alias.strip():
            raise QueryTypeError("Aggregation alias must be a non-empty string.")

    def _normalize_select_columns(
        self,
        columns: tuple[str | list[str] | tuple[str, ...], ...],
    ) -> list[str]:
        """
        Normalizes select() arguments.
        """

        if not columns:
            raise QueryError("select() requires at least one column.")

        if len(columns) == 1 and isinstance(columns[0], (list, tuple)):
            normalized = list(columns[0])
        else:
            normalized = list(columns)

        for column_name in normalized:
            if not isinstance(column_name, str):
                raise QueryTypeError("select() column names must be strings.")

        if len(normalized) != len(set(normalized)):
            raise QueryError("select() cannot contain duplicate columns.")

        return normalized

    def _validate_numeric_values(
        self,
        values: list[Any],
        column_name: str,
    ) -> list[int | float]:
        """
        Validates numeric values for sum/avg.
        """

        numeric_values: list[int | float] = []

        for value in values:
            if type(value) is bool or not isinstance(value, (int, float)):
                raise QueryTypeError(
                    f"Column '{column_name}' contains non-numeric value "
                    f"{value!r} of type {type(value).__name__}."
                )

            numeric_values.append(value)

        return numeric_values

    def _validate_non_negative_int(
        self,
        value: int,
        name: str,
    ) -> None:
        """
        Validates non-negative integer.
        """

        if type(value) is bool or not isinstance(value, int):
            raise QueryTypeError(f"{name} must be a non-negative integer.")

        if value < 0:
            raise QueryError(f"{name} cannot be negative.")

    def _row_distinct_key(
        self,
        row: dict[str, Any],
    ) -> tuple[tuple[str, Any], ...]:
        """
        Creates a hashable row key for DISTINCT.
        """

        return tuple(
            (column_name, self._make_hashable(row[column_name]))
            for column_name in row.keys()
        )

    def _make_hashable(self, value: Any) -> Any:
        """
        Converts values to hashable representations.
        """

        if isinstance(value, dict):
            return (
                "dict",
                tuple(
                    (key, self._make_hashable(value[key]))
                    for key in sorted(value.keys(), key=lambda item: repr(item))
                ),
            )

        if isinstance(value, list):
            return (
                "list",
                tuple(
                    self._make_hashable(item)
                    for item in value
                ),
            )

        if isinstance(value, tuple):
            return (
                "tuple",
                tuple(
                    self._make_hashable(item)
                    for item in value
                ),
            )

        if isinstance(value, set):
            return (
                "set",
                tuple(
                    sorted(
                        (self._make_hashable(item) for item in value),
                        key=lambda item: repr(item),
                    )
                ),
            )

        try:
            hash(value)
            return ("value", value)
        except TypeError:
            return ("repr", repr(value))

    def _json_default(self, value: Any) -> Any:
        """
        JSON serializer fallback.
        """

        if hasattr(self.source_query.db, "_json_default"):
            return self.source_query.db._json_default(value)

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
