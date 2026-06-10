from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import copy

from .exceptions import (
    ConstraintError,
    QueryTypeError,
    SchemaError,
)


INTERNAL_ROW_ID = "__relpy_row_id__"


@dataclass
class IndexDef:
    """
    Represents an index over one table.

    The index maps:
        index key -> stable internal row ids

    Example:
        index on orders.status

        {
            ("paid",): {1, 3, 7},
            ("pending",): {2, 4},
        }

    The values are internal row ids, not list positions.
    """

    name: str
    table_name: str
    columns: tuple[str, ...]
    unique: bool = False
    nulls_distinct: bool = True
    index_map: dict[tuple[Any, ...], set[int]] = field(default_factory=dict)


class IndexMixin:
    """
    Adds relational-style index support to RelPy.

    Indexes point to stable internal row ids, not list positions.
    """

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def create_index(
        self,
        table_name: str,
        columns: str | list[str] | tuple[str, ...],
        name: str | None = None,
        unique: bool = False,
        nulls_distinct: bool = True,
    ):
        """
        Creates an index on one table.

        Examples:
            db.create_index("orders", "status")
            db.create_index("orders", ["user_id", "status"])
            db.create_index("users", "email", unique=True)
        """

        self._validate_existing_table(table_name)

        if type(unique) is not bool:
            raise QueryTypeError("unique must be a bool.")

        if type(nulls_distinct) is not bool:
            raise QueryTypeError("nulls_distinct must be a bool.")

        normalized_columns = self._normalize_index_columns(
            table_name=table_name,
            columns=columns,
        )

        resolved_name = name or self._default_index_name(
            table_name=table_name,
            columns=normalized_columns,
            unique=unique,
        )

        self._validate_index_name(resolved_name)

        if resolved_name in self.indexes:
            raise SchemaError(f"Index '{resolved_name}' already exists.")

        self._ensure_table_row_ids(table_name)

        index_def = IndexDef(
            name=resolved_name,
            table_name=table_name,
            columns=normalized_columns,
            unique=unique,
            nulls_distinct=nulls_distinct,
        )

        self._rebuild_index(index_def)

        self.indexes[resolved_name] = index_def

        return self

    def drop_index(self, index_name: str):
        """
        Drops an index by name.
        """

        self._validate_index_name(index_name)

        if index_name not in self.indexes:
            raise SchemaError(f"Index '{index_name}' does not exist.")

        del self.indexes[index_name]

        return self

    def list_indexes(
        self,
        table_name: str | None = None,
    ) -> list[str]:
        """
        Lists index names.
        """

        if table_name is not None:
            self._validate_existing_table(table_name)

        return [
            index_name
            for index_name, index_def in self.indexes.items()
            if table_name is None or index_def.table_name == table_name
        ]

    def describe_index(self, index_name: str) -> dict[str, Any]:
        """
        Describes one index.
        """

        self._validate_index_name(index_name)

        if index_name not in self.indexes:
            raise SchemaError(f"Index '{index_name}' does not exist.")

        index_def = self.indexes[index_name]

        return {
            "name": index_def.name,
            "table_name": index_def.table_name,
            "columns": list(index_def.columns),
            "unique": index_def.unique,
            "nulls_distinct": index_def.nulls_distinct,
            "key_count": len(index_def.index_map),
            "row_id_count": sum(
                len(row_ids)
                for row_ids in index_def.index_map.values()
            ),
        }

    def describe_indexes(
        self,
        table_name: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """
        Describes all indexes.
        """

        return {
            index_name: self.describe_index(index_name)
            for index_name in self.list_indexes(table_name)
        }

    # -------------------------------------------------------------------------
    # Internal row id storage
    # -------------------------------------------------------------------------

    def _initialize_index_storage_for_table(self, table_name: str) -> None:
        """
        Initializes row-id storage metadata for a table.

        Should be called from create_table().
        """

        if not hasattr(self, "_next_row_id"):
            self._next_row_id: dict[str, int] = {}

        if not hasattr(self, "_row_positions"):
            self._row_positions: dict[str, dict[int, int]] = {}

        self._next_row_id[table_name] = 1
        self._row_positions[table_name] = {}

    def _ensure_table_row_ids(self, table_name: str) -> None:
        """
        Ensures every stored row has a stable internal row id.
        """

        if table_name not in self._next_row_id:
            self._next_row_id[table_name] = 1

        if table_name not in self._row_positions:
            self._row_positions[table_name] = {}

        changed = False

        for row in self.data[table_name]:
            if INTERNAL_ROW_ID not in row:
                row[INTERNAL_ROW_ID] = self._allocate_row_id(table_name)
                changed = True

        if changed or not self._row_positions[table_name]:
            self._refresh_row_positions(table_name)

    def _allocate_row_id(self, table_name: str) -> int:
        """
        Allocates a new stable internal row id.
        """

        next_id = self._next_row_id[table_name]
        self._next_row_id[table_name] += 1

        return next_id

    def _attach_row_id(self, table_name: str, row: dict[str, Any]) -> None:
        """
        Attaches an internal row id to a newly inserted row.
        """

        if INTERNAL_ROW_ID in row:
            raise SchemaError(
                f"Internal field '{INTERNAL_ROW_ID}' cannot be supplied by user code."
            )

        row[INTERNAL_ROW_ID] = self._allocate_row_id(table_name)

    def _refresh_row_positions(self, table_name: str) -> None:
        """
        Rebuilds row_id -> current list position mapping.

        Positions may change after delete.
        Row ids do not change.
        """

        self._row_positions[table_name] = {
            row[INTERNAL_ROW_ID]: position
            for position, row in enumerate(self.data[table_name])
        }

    def _row_id_from_row(self, row: dict[str, Any]) -> int:
        """
        Returns the internal row id of a stored row.
        """

        return row[INTERNAL_ROW_ID]

    def _stored_row_without_internal_fields(
        self,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Returns a copy of a stored row without internal metadata.
        """

        return {
            key: value
            for key, value in row.items()
            if key != INTERNAL_ROW_ID
        }

    # -------------------------------------------------------------------------
    # Index maintenance
    # -------------------------------------------------------------------------

    def _rebuild_index(self, index_def: IndexDef) -> None:
        """
        Rebuilds a single index from table data.

        Encrypted columns are indexed by blind_index, not by plaintext or ciphertext.
        """

        self._ensure_table_row_ids(index_def.table_name)

        index_map: dict[tuple[Any, ...], set[int]] = {}

        for row in self.data[index_def.table_name]:
            row_id = self._row_id_from_row(row)

            key = self._index_key_from_row(
                index_def=index_def,
                row=row,
            )

            row_ids = index_map.setdefault(key, set())
            row_ids.add(row_id)

            if self._violates_unique_index(
                index_def=index_def,
                key=key,
                row_ids=row_ids,
            ):
                raise ConstraintError(
                    f"Unique index '{index_def.name}' violation on table "
                    f"'{index_def.table_name}' for columns "
                    f"{list(index_def.columns)!r}."
                )

        index_def.index_map = index_map

    def _rebuild_table_indexes(self, table_name: str) -> None:
        """
        Rebuilds all indexes for one table.
        """

        if not hasattr(self, "indexes"):
            return

        self._ensure_table_row_ids(table_name)
        self._refresh_row_positions(table_name)

        for index_def in self.indexes.values():
            if index_def.table_name == table_name:
                self._rebuild_index(index_def)

    def _rebuild_all_indexes(self) -> None:
        """
        Rebuilds all indexes.

        Use after cascading operations that may touch multiple tables.
        """

        if not hasattr(self, "indexes"):
            return

        for table_name in self.data.keys():
            self._ensure_table_row_ids(table_name)
            self._refresh_row_positions(table_name)

        for index_def in self.indexes.values():
            self._rebuild_index(index_def)

    def _add_row_to_table_indexes(
        self,
        table_name: str,
        row: dict[str, Any],
    ) -> None:
        """
        Adds one newly inserted stored row to all indexes of a table.

        This is much cheaper than rebuilding all table indexes after every
        insert(). If a unique index is violated, the caller is expected to roll
        back the row append and rebuild indexes.
        """

        if not hasattr(self, "indexes"):
            return

        self._ensure_table_row_ids(table_name)
        row_id = self._row_id_from_row(row)

        for index_def in self.indexes.values():
            if index_def.table_name != table_name:
                continue

            key = self._index_key_from_row(
                index_def=index_def,
                row=row,
            )

            row_ids = index_def.index_map.setdefault(key, set())
            row_ids.add(row_id)

            if self._violates_unique_index(
                index_def=index_def,
                key=key,
                row_ids=row_ids,
            ):
                row_ids.remove(row_id)

                if not row_ids:
                    del index_def.index_map[key]

                raise ConstraintError(
                    f"Unique index '{index_def.name}' violation on table "
                    f"'{index_def.table_name}' for columns "
                    f"{list(index_def.columns)!r}."
                )

    def _lookup_indexed_row_ids(
        self,
        table_name: str,
        equality_criteria: dict[str, Any],
    ) -> set[int] | None:
        """
        Attempts to use an index for equality criteria.

        Encrypted equality values are converted to blind-index lookup keys.
        """

        if not equality_criteria:
            return None

        if not hasattr(self, "indexes"):
            return None

        candidates: list[tuple[int, IndexDef]] = []

        for index_def in self.indexes.values():
            if index_def.table_name != table_name:
                continue

            prefix_length = 0

            for column_name in index_def.columns:
                if column_name in equality_criteria:
                    prefix_length += 1
                else:
                    break

            if prefix_length > 0:
                candidates.append((prefix_length, index_def))

        if not candidates:
            return None

        best_prefix_length, best_index = max(
            candidates,
            key=lambda item: item[0],
        )

        key_prefix = tuple(
            self._lookup_value_for_index(
                table_name=table_name,
                column_name=column_name,
                value=equality_criteria[column_name],
            )
            for column_name in best_index.columns[:best_prefix_length]
        )

        if best_prefix_length == len(best_index.columns):
            return set(best_index.index_map.get(key_prefix, set()))

        matching_row_ids: set[int] = set()

        for index_key, row_ids in best_index.index_map.items():
            if index_key[:best_prefix_length] == key_prefix:
                matching_row_ids.update(row_ids)

        return matching_row_ids

    def _load_rows_by_row_ids(
        self,
        table_name: str,
        row_ids: set[int],
    ) -> list[dict[str, Any]]:
        """
        Loads stored rows by stable internal row ids.

        Internal row ids are removed, but encrypted payloads are preserved so query
        execution can still decrypt or mask them later.
        """

        self._refresh_row_positions(table_name)

        rows_with_positions: list[tuple[int, dict[str, Any]]] = []

        for row_id in row_ids:
            position = self._row_positions[table_name].get(row_id)

            if position is None:
                continue

            stored_row = self.data[table_name][position]
            rows_with_positions.append((position, stored_row))

        rows_with_positions.sort(key=lambda item: item[0])

        return [
            self._stored_row_without_internal_fields(row)
            for _, row in rows_with_positions
        ]

    # -------------------------------------------------------------------------
    # Validation and helpers
    # -------------------------------------------------------------------------

    def _violates_unique_index(
        self,
        index_def: IndexDef,
        key: tuple[Any, ...],
        row_ids: set[int],
    ) -> bool:
        """
        Checks whether a unique index is violated.

        With nulls_distinct=True, keys containing None do not violate uniqueness.
        This is similar to common SQL behavior.
        """

        if not index_def.unique:
            return False

        if index_def.nulls_distinct and self._index_key_contains_null(key):
            return False

        return len(row_ids) > 1

    def _index_key_contains_null(self, key: tuple[Any, ...]) -> bool:
        """
        Checks whether an index key contains None.
        """

        return any(value == ("value", None) for value in key)

    def _normalize_index_columns(
        self,
        table_name: str,
        columns: str | list[str] | tuple[str, ...],
    ) -> tuple[str, ...]:
        """
        Normalizes index columns.
        """

        if isinstance(columns, str):
            if not columns.strip():
                raise QueryTypeError("Index column name cannot be empty.")

            normalized = [columns]

        elif isinstance(columns, (list, tuple)):
            if not columns:
                raise SchemaError("Index columns cannot be empty.")

            normalized = list(columns)

            for column_name in normalized:
                if not isinstance(column_name, str) or not column_name.strip():
                    raise QueryTypeError("Index columns must be non-empty strings.")

        else:
            raise QueryTypeError(
                "columns must be a string, list of strings, or tuple of strings."
            )

        if len(normalized) != len(set(normalized)):
            raise SchemaError("Index columns cannot contain duplicates.")

        for column_name in normalized:
            self._validate_existing_column(table_name, column_name)

        return tuple(normalized)

    def _validate_index_name(self, index_name: str) -> None:
        """
        Validates index name.
        """

        if not isinstance(index_name, str) or not index_name.strip():
            raise QueryTypeError("Index name must be a non-empty string.")

        if hasattr(self, "_validate_name"):
            self._validate_name(index_name, "Index")

    def _default_index_name(
        self,
        table_name: str,
        columns: tuple[str, ...],
        unique: bool,
    ) -> str:
        """
        Builds a default index name.
        """

        prefix = "uidx" if unique else "idx"

        return f"{prefix}_{table_name}_{'_'.join(columns)}"

    def _index_value_for_row(
        self,
        *,
        table_name: str,
        column_name: str,
        row: dict[str, Any],
    ) -> Any:
        """
        Returns the value used in an index key for one stored row.

        Encrypted columns use the stored blind index.
        """

        column_def = self.schema[table_name].columns[column_name]
        value = row.get(column_name)

        if column_def.is_encrypted:
            if value is None:
                return self._make_index_hashable(None)

            if not self._is_encrypted_payload(value):
                raise ConstraintError(
                    f"Encrypted column '{column_name}' contains a non-encrypted value."
                )

            return ("encrypted_blind_index", value["blind_index"])

        return self._make_index_hashable(value)


    def _lookup_value_for_index(
        self,
        *,
        table_name: str,
        column_name: str,
        value: Any,
    ) -> Any:
        """
        Returns the value used to look up one indexed equality criterion.
        """

        column_def = self.schema[table_name].columns[column_name]

        if column_def.is_encrypted:
            if value is None:
                return self._make_index_hashable(None)

            blind_index = self._encrypted_index_lookup_value(
                table_name=table_name,
                column_name=column_name,
                value=value,
            )

            return ("encrypted_blind_index", blind_index)

        return self._make_index_hashable(value)

    def _index_key_from_row(
        self,
        index_def: IndexDef,
        row: dict[str, Any],
    ) -> tuple[Any, ...]:
        """
        Creates an index key from a stored row.
        """

        return tuple(
            self._index_value_for_row(
                table_name=index_def.table_name,
                column_name=column_name,
                row=row,
            )
            for column_name in index_def.columns
        )

    def _make_index_hashable(self, value: Any) -> Any:
        """
        Converts values into hashable representations.
        """

        if isinstance(value, dict):
            return (
                "dict",
                tuple(
                    (key, self._make_index_hashable(value[key]))
                    for key in sorted(value.keys(), key=lambda item: repr(item))
                ),
            )

        if isinstance(value, list):
            return (
                "list",
                tuple(
                    self._make_index_hashable(item)
                    for item in value
                ),
            )

        if isinstance(value, tuple):
            return (
                "tuple",
                tuple(
                    self._make_index_hashable(item)
                    for item in value
                ),
            )

        if isinstance(value, set):
            return (
                "set",
                tuple(
                    sorted(
                        (
                            self._make_index_hashable(item)
                            for item in value
                        ),
                        key=lambda item: repr(item),
                    )
                ),
            )

        try:
            hash(value)
            return ("value", value)
        except TypeError:
            return ("repr", repr(value))
