from __future__ import annotations

from typing import Any

from .exceptions import (
    ColumnNotFoundError,
    QueryError,
    QueryTypeError,
)


JOIN_METHOD_ALIASES = {
    "regular": "inner",
    "join": "inner",
    "inner": "inner",
    "inner_join": "inner",

    "left": "left",
    "left_join": "left",
    "left_outer": "left",
    "left_outer_join": "left",

    "right": "right",
    "right_join": "right",
    "right_outer": "right",
    "right_outer_join": "right",

    "full": "full",
    "full_join": "full",
    "outer": "full",
    "full_outer": "full",
    "full_outer_join": "full",

    "cross": "cross",
    "cross_join": "cross",
}


def join_queries(
    left_query: Any,
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
):
    """
    Joins a Query with another table or Query.

    Supported join types:
        regular -> alias for inner
        inner
        left
        right
        full
        cross

    Examples:
        db.query("orders").join("users", on=("user_id", "id"))

        db.query("orders").join(
            "users",
            left_on="user_id",
            right_on="id",
            how="left",
        )

        db.query("orders").join(
            db.query("users").where(col("status") == "active"),
            on=("user_id", "id"),
            how="inner",
        )
    """

    from .queries import Query

    if not isinstance(left_query, Query):
        raise QueryTypeError("left_query must be a Query object.")

    right_query = _resolve_right_query(left_query, other)
    join_type = _normalize_join_type(method)

    join_pairs = _normalize_join_pairs(
        on=on,
        left_on=left_on,
        right_on=right_on,
        join_type=join_type,
    )

    if join_type != "cross" and not join_pairs:
        join_pairs = _infer_join_pairs_from_foreign_keys(
            left_query=left_query,
            right_query=right_query,
        )

    if join_type == "natural":
        if on is not None or left_on is not None or right_on is not None:
            raise QueryError("natural join cannot receive on=... or left_on/right_on.")

        join_pairs = _infer_natural_join_pairs(
            left_query=left_query,
            right_query=right_query,
        )

        execution_join_type = "inner"

    else:
        join_pairs = _normalize_join_pairs(
            on=on,
            left_on=left_on,
            right_on=right_on,
            join_type=join_type
        )

        if join_type != "cross" and not join_pairs:
            join_pairs = _infer_join_pairs_from_foreign_keys(
                left_query=left_query,
                right_query=right_query,
            )

        execution_join_type = join_type

    left_columns = left_query._result_columns()
    right_columns = right_query._result_columns()

    if join_type != "cross":
        _validate_join_columns(
            columns=left_columns,
            join_columns=[left for left, _ in join_pairs],
            side_name="left",
            relation_name=left_query._relation_display_name(),
        )

        _validate_join_columns(
            columns=right_columns,
            join_columns=[right for _, right in join_pairs],
            side_name="right",
            relation_name=right_query._relation_display_name(),
        )

    left_rows = left_query._execute_rows(project=True, apply_order=True)
    right_rows = right_query._execute_rows(project=True, apply_order=True)

    left_name = left_query._relation_display_name()
    right_name = right_query._relation_display_name()

    left_column_map, right_column_map, output_columns = _build_output_column_maps(
        left_columns=left_columns,
        right_columns=right_columns,
        left_name=left_name,
        right_name=right_name,
        left_prefix=left_prefix,
        right_prefix=right_prefix,
        prefix_columns=prefix_columns,
        preserve_prefixed_columns=preserve_prefixed_columns,
        suffixes=suffixes,
    )

    if join_type == "cross":
        joined_rows = _cross_join_rows(
            left_rows=left_rows,
            right_rows=right_rows,
            left_column_map=left_column_map,
            right_column_map=right_column_map,
        )
    else:
        joined_rows = _join_rows(
            left_rows=left_rows,
            right_rows=right_rows,
            join_pairs=join_pairs,
            join_type=join_type,
            left_column_map=left_column_map,
            right_column_map=right_column_map,
            match_nulls=match_nulls,
        )

    relation_name = f"{left_name}_{join_type}_join_{right_name}"

    return Query.from_rows(
        db=left_query.db,
        relation_name=relation_name,
        rows=joined_rows,
        columns=output_columns,
    )


# =============================================================================
# Right query resolution
# =============================================================================

def _resolve_right_query(left_query: Any, other: Any):
    """
    Resolves the right side of the join.

    other can be:
        - table name string
        - view name string
        - Query object
    """

    from .queries import Query

    if isinstance(other, Query):
        return other

    if isinstance(other, str):
        if not other.strip():
            raise QueryTypeError("join target table/view name cannot be empty.")

        db = left_query.db

        if hasattr(db, "views") and other in db.views:
            return db.view(other)

        return db.query(other)

    raise QueryTypeError(
        "join target must be a table/view name string or a Query object."
    )


# =============================================================================
# Join type and join pair normalization
# =============================================================================

def _normalize_join_type(method: str) -> str:
    """
    Normalizes join type.

    Regular is treated as INNER.
    """

    if not isinstance(method, str) or not method.strip():
        raise QueryTypeError("join how must be a non-empty string.")

    normalized = (
        method.strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )

    if normalized not in JOIN_METHOD_ALIASES:
        raise QueryError(
            f"Unsupported join type '{method}'. "
            "Supported types: regular, inner, left, right, full, cross."
        )

    return JOIN_METHOD_ALIASES[normalized]


def _normalize_join_pairs(
    *,
    on: str | tuple[str, str] | list[tuple[str, str]] | tuple[tuple[str, str], ...] | None,
    left_on: str | list[str] | tuple[str, ...] | None,
    right_on: str | list[str] | tuple[str, ...] | None,
    join_type: str,
) -> tuple[tuple[str, str], ...]:
    """
    Normalizes join columns.

    Supports:
        on="id"                         -> id = id
        on=("user_id", "id")            -> user_id = id
        on=[("a", "b"), ("c", "d")]     -> composite join

        left_on="user_id", right_on="id"
        left_on=["a", "c"], right_on=["b", "d"]
    """

    if join_type == "cross":
        if on is not None or left_on is not None or right_on is not None:
            raise QueryError("cross join cannot receive join columns.")

        return tuple()

    using_on = on is not None
    using_left_right = left_on is not None or right_on is not None

    if using_on and using_left_right:
        raise QueryError("Use either on=... or left_on/right_on, not both.")

    if using_on:
        return _normalize_on_argument(on)

    if using_left_right:
        if left_on is None or right_on is None:
            raise QueryError("left_on and right_on must be provided together.")

        left_columns = _normalize_column_list(left_on, "left_on")
        right_columns = _normalize_column_list(right_on, "right_on")

        if len(left_columns) != len(right_columns):
            raise QueryError("left_on and right_on must have the same length.")

        return tuple(zip(left_columns, right_columns))

    # No explicit join columns.
    # The caller will try to infer the join from foreign keys.
    return tuple()


def _normalize_on_argument(
    on: str | tuple[str, str] | list[tuple[str, str]] | tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """
    Normalizes the on= argument.
    """

    if isinstance(on, str):
        if not on.strip():
            raise QueryTypeError("on column name cannot be empty.")

        return ((on, on),)

    if (
        isinstance(on, tuple)
        and len(on) == 2
        and isinstance(on[0], str)
        and isinstance(on[1], str)
    ):
        left_column, right_column = on

        if not left_column.strip() or not right_column.strip():
            raise QueryTypeError("join column names cannot be empty.")

        return ((left_column, right_column),)

    if isinstance(on, (list, tuple)):
        pairs: list[tuple[str, str]] = []

        for item in on:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
            ):
                raise QueryTypeError(
                    "on must contain pairs of strings, e.g. "
                    "[('tenant_id', 'tenant_id'), ('user_id', 'id')]."
                )

            left_column, right_column = item

            if not left_column.strip() or not right_column.strip():
                raise QueryTypeError("join column names cannot be empty.")

            pairs.append((left_column, right_column))

        if not pairs:
            raise QueryError("on cannot be an empty list.")

        return tuple(pairs)

    raise QueryTypeError(
        "on must be a string, a pair of strings, or a list of pairs."
    )


def _normalize_column_list(
    value: str | list[str] | tuple[str, ...],
    name: str,
) -> list[str]:
    """
    Normalizes left_on/right_on column lists.
    """

    if isinstance(value, str):
        if not value.strip():
            raise QueryTypeError(f"{name} column name cannot be empty.")

        return [value]

    if isinstance(value, (list, tuple)):
        if not value:
            raise QueryError(f"{name} cannot be empty.")

        normalized = []

        for column_name in value:
            if not isinstance(column_name, str) or not column_name.strip():
                raise QueryTypeError(f"{name} must contain non-empty strings.")

            normalized.append(column_name)

        return normalized

    raise QueryTypeError(f"{name} must be a string, list, or tuple.")


def _validate_join_columns(
    *,
    columns: list[str],
    join_columns: list[str],
    side_name: str,
    relation_name: str,
) -> None:
    """
    Validates that join columns exist.
    """

    available = set(columns)

    for column_name in join_columns:
        if column_name not in available:
            raise ColumnNotFoundError(
                f"Join column '{column_name}' does not exist on {side_name} "
                f"relation '{relation_name}'."
            )


# =============================================================================
# Output columns
# =============================================================================

def _build_output_column_maps(
    *,
    left_columns: list[str],
    right_columns: list[str],
    left_name: str,
    right_name: str,
    left_prefix: str | None,
    right_prefix: str | None,
    prefix_columns: bool,
    preserve_prefixed_columns: bool,
    suffixes: tuple[str, str],
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """
    Builds source-column -> output-column maps.

    Default behavior prefixes columns:
        orders.id
        users.id

    If a column already contains '.', it is preserved by default.
    This makes chained joins readable:
        orders.id stays orders.id
        users.id stays users.id
        products.id becomes products.id
    """

    _validate_suffixes(suffixes)

    if type(prefix_columns) is not bool:
        raise QueryTypeError("prefix_columns must be a bool.")

    if type(preserve_prefixed_columns) is not bool:
        raise QueryTypeError("preserve_prefixed_columns must be a bool.")

    if prefix_columns:
        resolved_left_prefix = left_prefix or left_name
        resolved_right_prefix = right_prefix or right_name

        if not isinstance(resolved_left_prefix, str) or not resolved_left_prefix.strip():
            raise QueryTypeError("left_prefix must be a non-empty string.")

        if not isinstance(resolved_right_prefix, str) or not resolved_right_prefix.strip():
            raise QueryTypeError("right_prefix must be a non-empty string.")

        if resolved_left_prefix == resolved_right_prefix:
            resolved_left_prefix = f"{resolved_left_prefix}{suffixes[0]}"
            resolved_right_prefix = f"{resolved_right_prefix}{suffixes[1]}"

        left_map = {
            column_name: _prefixed_column_name(
                column_name=column_name,
                prefix=resolved_left_prefix,
                preserve_prefixed_columns=preserve_prefixed_columns,
            )
            for column_name in left_columns
        }

        right_map = {
            column_name: _prefixed_column_name(
                column_name=column_name,
                prefix=resolved_right_prefix,
                preserve_prefixed_columns=preserve_prefixed_columns,
            )
            for column_name in right_columns
        }

    else:
        left_collision_suffix, right_collision_suffix = suffixes
        collisions = set(left_columns) & set(right_columns)

        left_map = {
            column_name: (
                f"{column_name}{left_collision_suffix}"
                if column_name in collisions
                else column_name
            )
            for column_name in left_columns
        }

        right_map = {
            column_name: (
                f"{column_name}{right_collision_suffix}"
                if column_name in collisions
                else column_name
            )
            for column_name in right_columns
        }

    output_columns = list(left_map.values()) + list(right_map.values())

    if len(output_columns) != len(set(output_columns)):
        raise QueryError(
            "Join output contains duplicate column names. "
            "Use left_prefix/right_prefix or disable preserve_prefixed_columns."
        )

    return left_map, right_map, output_columns


def _prefixed_column_name(
    *,
    column_name: str,
    prefix: str,
    preserve_prefixed_columns: bool,
) -> str:
    """
    Returns a prefixed output column name.
    """

    if preserve_prefixed_columns and "." in column_name:
        return column_name

    return f"{prefix}.{column_name}"


def _validate_suffixes(suffixes: tuple[str, str]) -> None:
    """
    Validates suffixes.
    """

    if (
        not isinstance(suffixes, tuple)
        or len(suffixes) != 2
        or not isinstance(suffixes[0], str)
        or not isinstance(suffixes[1], str)
    ):
        raise QueryTypeError("suffixes must be a tuple of two strings.")

    if suffixes[0] == suffixes[1]:
        raise QueryError("suffixes must be different.")


# =============================================================================
# Row joining
# =============================================================================

def _join_rows(
    *,
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    join_pairs: tuple[tuple[str, str], ...],
    join_type: str,
    left_column_map: dict[str, str],
    right_column_map: dict[str, str],
    match_nulls: bool,
) -> list[dict[str, Any]]:
    """
    Performs inner/left/right/full join.
    """

    left_join_columns = [left for left, _ in join_pairs]
    right_join_columns = [right for _, right in join_pairs]

    right_index: dict[tuple[Any, ...], list[tuple[int, dict[str, Any]]]] = {}

    for right_index_number, right_row in enumerate(right_rows):
        key = _join_key(
            row=right_row,
            columns=right_join_columns,
            match_nulls=match_nulls,
        )

        if key is None:
            continue

        right_index.setdefault(key, []).append((right_index_number, right_row))

    joined_rows: list[dict[str, Any]] = []
    matched_right_indices: set[int] = set()

    right_null_part = _null_part(right_column_map)
    left_null_part = _null_part(left_column_map)

    for left_row in left_rows:
        key = _join_key(
            row=left_row,
            columns=left_join_columns,
            match_nulls=match_nulls,
        )

        matches = [] if key is None else right_index.get(key, [])

        if matches:
            for right_index_number, right_row in matches:
                matched_right_indices.add(right_index_number)

                joined_rows.append(
                    _combine_rows(
                        left_row=left_row,
                        right_row=right_row,
                        left_column_map=left_column_map,
                        right_column_map=right_column_map,
                    )
                )

        elif join_type in {"left", "full"}:
            joined_rows.append(
                {
                    **_project_row(left_row, left_column_map),
                    **right_null_part,
                }
            )

    if join_type in {"right", "full"}:
        for right_index_number, right_row in enumerate(right_rows):
            if right_index_number in matched_right_indices:
                continue

            joined_rows.append(
                {
                    **left_null_part,
                    **_project_row(right_row, right_column_map),
                }
            )

    return joined_rows


def _cross_join_rows(
    *,
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    left_column_map: dict[str, str],
    right_column_map: dict[str, str],
) -> list[dict[str, Any]]:
    """
    Performs CROSS JOIN.
    """

    joined_rows: list[dict[str, Any]] = []

    for left_row in left_rows:
        for right_row in right_rows:
            joined_rows.append(
                _combine_rows(
                    left_row=left_row,
                    right_row=right_row,
                    left_column_map=left_column_map,
                    right_column_map=right_column_map,
                )
            )

    return joined_rows


def _combine_rows(
    *,
    left_row: dict[str, Any],
    right_row: dict[str, Any],
    left_column_map: dict[str, str],
    right_column_map: dict[str, str],
) -> dict[str, Any]:
    """
    Combines one left row and one right row.
    """

    return {
        **_project_row(left_row, left_column_map),
        **_project_row(right_row, right_column_map),
    }


def _project_row(
    row: dict[str, Any],
    column_map: dict[str, str],
) -> dict[str, Any]:
    """
    Projects row columns using an output column map.
    """

    return {
        output_column: row[source_column]
        for source_column, output_column in column_map.items()
    }


def _null_part(
    column_map: dict[str, str],
) -> dict[str, None]:
    """
    Creates null values for unmatched outer join rows.
    """

    return {
        output_column: None
        for output_column in column_map.values()
    }


def _join_key(
    *,
    row: dict[str, Any],
    columns: list[str],
    match_nulls: bool,
) -> tuple[Any, ...] | None:
    """
    Creates a hashable join key.

    By default, None does not match None, like SQL NULL behavior.
    """

    values = []

    for column_name in columns:
        value = row[column_name]

        if value is None and not match_nulls:
            return None

        values.append(_make_hashable(value))

    return tuple(values)


def _make_hashable(value: Any) -> Any:
    """
    Converts values into hashable representations.
    """

    if isinstance(value, dict):
        return (
            "dict",
            tuple(
                (key, _make_hashable(value[key]))
                for key in sorted(value.keys(), key=lambda item: repr(item))
            ),
        )

    if isinstance(value, list):
        return (
            "list",
            tuple(
                _make_hashable(item)
                for item in value
            ),
        )

    if isinstance(value, tuple):
        return (
            "tuple",
            tuple(
                _make_hashable(item)
                for item in value
            ),
        )

    if isinstance(value, set):
        return (
            "set",
            tuple(
                sorted(
                    (_make_hashable(item) for item in value),
                    key=lambda item: repr(item),
                )
            ),
        )

    try:
        hash(value)
        return ("value", value)
    except TypeError:
        return ("repr", repr(value))


def _infer_join_pairs_from_foreign_keys(
    *,
    left_query: Any,
    right_query: Any,
) -> tuple[tuple[str, str], ...]:
    """
    Infers join columns from declared foreign keys.

    Important:
        This function only uses a foreign key if the target table of that
        foreign key is actually represented on the opposite side of the join.

    Example:
        db.query("orders").join("order_items")

    Correct inference:
        orders.id = order_items.order_id

    Incorrect inference prevented:
        orders.id = order_items.product_id
        because product_id references products.id, not orders.id.
    """

    db = left_query.db

    left_relation_name = left_query._relation_display_name()
    right_relation_name = right_query._relation_display_name()

    right_table_name = getattr(right_query, "table_name", None)

    candidates: list[tuple[str, str]] = []

    # -------------------------------------------------------------------------
    # Case 1:
    # A table represented on the left side has a FK to the right table.
    #
    # Example:
    # db.query("orders").join("customers")
    #
    # orders.customer_id -> customers.id
    #
    # left column:
    #   customer_id
    # or:
    #   orders.customer_id
    #
    # right column:
    #   id
    # -------------------------------------------------------------------------

    if right_table_name is not None:
        for local_table_name, table_def in db.schema.items():
            if not _query_represents_table(left_query, local_table_name):
                continue

            for foreign_key in table_def.foreign_keys.values():
                if foreign_key.target_table != right_table_name:
                    continue

                left_candidates = _query_column_candidates_for_table(
                    query=left_query,
                    table_name=local_table_name,
                    column_name=foreign_key.local_column,
                )

                right_candidates = _query_column_candidates_for_table(
                    query=right_query,
                    table_name=right_table_name,
                    column_name=foreign_key.target_column,
                )

                for left_column in left_candidates:
                    for right_column in right_candidates:
                        candidates.append((left_column, right_column))

    # -------------------------------------------------------------------------
    # Case 2:
    # The right table has a FK to a table represented on the left side.
    #
    # Example:
    # db.query("orders").join("order_items")
    #
    # order_items.order_id -> orders.id
    #
    # left column:
    #   id
    # or:
    #   orders.id
    #
    # right column:
    #   order_id
    # -------------------------------------------------------------------------

    if right_table_name is not None and right_table_name in db.schema:
        right_table_def = db.schema[right_table_name]

        for foreign_key in right_table_def.foreign_keys.values():
            target_table_name = foreign_key.target_table

            # Critical check:
            # Do not use product_id -> products.id unless products is actually
            # represented on the left side.
            if not _query_represents_table(left_query, target_table_name):
                continue

            left_candidates = _query_column_candidates_for_table(
                query=left_query,
                table_name=target_table_name,
                column_name=foreign_key.target_column,
            )

            right_candidates = _query_column_candidates_for_table(
                query=right_query,
                table_name=right_table_name,
                column_name=foreign_key.local_column,
            )

            for left_column in left_candidates:
                for right_column in right_candidates:
                    candidates.append((left_column, right_column))

    unique_candidates: list[tuple[str, str]] = []

    for candidate in candidates:
        if candidate not in unique_candidates:
            unique_candidates.append(candidate)

    if not unique_candidates:
        raise QueryError(
            f"Could not infer join columns between "
            f"'{left_relation_name}' and '{right_relation_name}'. "
            "Provide on=... or left_on/right_on explicitly."
        )

    if len(unique_candidates) > 1:
        raise QueryError(
            f"Ambiguous join between '{left_relation_name}' and "
            f"'{right_relation_name}'. Found multiple possible join paths: "
            f"{unique_candidates!r}. Provide on=... explicitly."
        )

    return tuple(unique_candidates)


def _infer_natural_join_pairs(
    *,
    left_query: Any,
    right_query: Any,
) -> tuple[tuple[str, str], ...]:
    """
    Infers NATURAL JOIN columns.

    NATURAL JOIN uses columns with the same names on both sides.

    Important:
        This does not use foreign keys.
        It follows SQL NATURAL JOIN behavior.
    """

    left_columns = left_query._result_columns()
    right_columns = right_query._result_columns()

    right_column_set = set(right_columns)

    common_columns = [
        column_name
        for column_name in left_columns
        if column_name in right_column_set
    ]

    if not common_columns:
        raise QueryError(
            f"Could not perform natural join between "
            f"'{left_query._relation_display_name()}' and "
            f"'{right_query._relation_display_name()}'. "
            "No common column names were found."
        )

    return tuple(
        (column_name, column_name)
        for column_name in common_columns
    )

def _query_represents_table(
    query: Any,
    table_name: str,
) -> bool:
    """
    Returns True if a query result clearly represents a given table.

    Cases:
        db.query("orders")
            represents orders

        db.query("orders").join("users")
            has columns like orders.id, users.id
            represents orders and users
    """

    if getattr(query, "table_name", None) == table_name:
        return True

    prefix = f"{table_name}."

    return any(
        column_name.startswith(prefix)
        for column_name in query._result_columns()
    )


def _query_column_candidates_for_table(
    query: Any,
    table_name: str,
    column_name: str,
) -> list[str]:
    """
    Returns possible column names for a table column inside a query result.

    For base query:
        db.query("orders")
        column id appears as:
            id

    For joined query:
        db.query("orders").join("users")
        column id appears as:
            orders.id
            users.id
    """

    result_columns = query._result_columns()
    candidates: list[str] = []

    unprefixed_column = column_name
    prefixed_column = f"{table_name}.{column_name}"

    if getattr(query, "table_name", None) == table_name:
        if unprefixed_column in result_columns:
            candidates.append(unprefixed_column)

    if prefixed_column in result_columns:
        candidates.append(prefixed_column)

    return candidates