from __future__ import annotations

import copy
from typing import Any, Callable

from .indexes import INTERNAL_ROW_ID


# =============================================================================
# CRUD mixin
# =============================================================================

class CrudMixin:
    """
    Provides RelPy's row-level data API: insert, insert_many, update, delete.

    This mixin is responsible for:
    - Validating rows against the schema (types, nullability, defaults).
    - Filling in AutoNumber values and column defaults.
    - Enforcing primary key and foreign key constraints.
    - Applying ON DELETE rules (RESTRICT / CASCADE / SET NULL).
    - Keeping indexes and the primary-key lookup cache consistent, with
      automatic rollback if any step fails partway through.
    """

    # -------------------------------------------------------------------------
    # Public data API
    # -------------------------------------------------------------------------

    def insert(
        self,
        table_name: str,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Inserts a new row into a table.

        Optimized behavior:
        - Validates the plaintext row before storage.
        - Encrypts encrypted columns only after validation.
        - Updates existing indexes incrementally instead of rebuilding all table
          indexes after every insert.
        - Updates the primary-key lookup cache incrementally.

        For loading many rows, prefer insert_many().
        """

        self._validate_existing_table(table_name)
        self._validate_row_dict(row, "row")

        normalized_row, pending_auto_sequences = self._build_insert_row(
            table_name=table_name,
            input_row=row,
        )

        self._validate_complete_row_against_schema(
            table_name=table_name,
            row=normalized_row,
        )

        self._validate_primary_key_for_candidate(
            table_name=table_name,
            candidate_row=normalized_row,
        )

        self._validate_foreign_keys_for_row(
            table_name=table_name,
            row=normalized_row,
        )

        storage_row = self._encrypt_row_for_storage(
            table_name=table_name,
            row=normalized_row,
        )

        old_next_row_id = self._next_row_id[table_name]
        old_auto_sequences = dict(self.schema[table_name].auto_sequences)
        row_was_appended = False

        try:
            for column_name, sequence_value in pending_auto_sequences.items():
                self.schema[table_name].auto_sequences[column_name] = sequence_value

            self._attach_row_id(table_name, storage_row)
            self.data[table_name].append(storage_row)
            row_was_appended = True

            row_id = storage_row[INTERNAL_ROW_ID]
            self._row_positions[table_name][row_id] = len(self.data[table_name]) - 1

            self._add_row_to_table_indexes(table_name, storage_row)
            self._add_row_to_primary_key_lookup(table_name, storage_row)

        except Exception:
            if row_was_appended:
                self.data[table_name].pop()

            self._next_row_id[table_name] = old_next_row_id
            self.schema[table_name].auto_sequences = old_auto_sequences
            self._refresh_row_positions(table_name)
            self._rebuild_table_indexes(table_name)
            self._refresh_primary_key_lookup(table_name)
            raise

        return self._stored_row_to_python_dict(
            storage_row,
            table_name=table_name,
            decrypt=True,
        )

    def insert_many(
        self,
        table_name: str,
        rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> list[dict[str, Any]]:
        """
        Inserts many rows into a table in one batch.

        This is much faster than calling insert() in a loop because it:
        - validates all rows first,
        - commits AutoNumber sequences once,
        - appends all rows in one batch,
        - rebuilds indexes once,
        - refreshes row positions once,
        - refreshes the primary-key lookup once.

        Example:
            db.insert_many("registrations", registration_rows)
        """

        self._validate_existing_table(table_name)

        if not isinstance(rows, (list, tuple)):
            raise TypeError("rows must be a list or tuple of dictionaries.")

        if not rows:
            return []

        for index, row in enumerate(rows):
            self._validate_row_dict(row, f"rows[{index}]")

        table_ref = self.schema[table_name]
        working_auto_sequences = dict(table_ref.auto_sequences)
        normalized_rows: list[dict[str, Any]] = []

        for input_row in rows:
            self._validate_known_columns(
                table_name=table_name,
                values=input_row,
                context="insert_many",
            )

            complete_row: dict[str, Any] = {}

            for column_name, column_def in table_ref.columns.items():
                if column_name in input_row:
                    value = input_row[column_name]

                    if column_def.is_auto_number and value is not None:
                        if not self._is_value_compatible(value, int):
                            raise TypeError(
                                f"AutoNumber column '{table_name}.{column_name}' "
                                f"must receive an int value, got {type(value).__name__}."
                            )

                        working_auto_sequences[column_name] = max(
                            working_auto_sequences.get(column_name, 0),
                            value,
                        )

                    complete_row[column_name] = self._clone_default(value)
                    continue

                if column_def.is_auto_number:
                    next_value = working_auto_sequences.get(column_name, 0) + 1
                    working_auto_sequences[column_name] = next_value
                    complete_row[column_name] = next_value
                    continue

                if column_def.has_default:
                    complete_row[column_name] = self._clone_default(column_def.default)
                    continue

                if column_def.nullable:
                    complete_row[column_name] = None
                    continue

                raise ValueError(
                    f"Missing required value for non-nullable column "
                    f"'{table_name}.{column_name}'."
                )

            self._validate_complete_row_against_schema(
                table_name=table_name,
                row=complete_row,
            )

            self._validate_foreign_keys_for_row(
                table_name=table_name,
                row=complete_row,
            )

            normalized_rows.append(complete_row)

        self._validate_primary_keys_for_batch(
            table_name=table_name,
            candidate_rows=normalized_rows,
        )

        storage_rows = [
            self._encrypt_row_for_storage(
                table_name=table_name,
                row=row,
            )
            for row in normalized_rows
        ]

        old_rows = list(self.data[table_name])
        old_next_row_id = self._next_row_id[table_name]
        old_auto_sequences = dict(table_ref.auto_sequences)

        try:
            table_ref.auto_sequences = working_auto_sequences

            for storage_row in storage_rows:
                self._attach_row_id(table_name, storage_row)

            self.data[table_name].extend(storage_rows)
            self._refresh_row_positions(table_name)
            self._rebuild_table_indexes(table_name)
            self._refresh_primary_key_lookup(table_name)

        except Exception:
            self.data[table_name] = old_rows
            self._next_row_id[table_name] = old_next_row_id
            table_ref.auto_sequences = old_auto_sequences
            self._refresh_row_positions(table_name)
            self._rebuild_table_indexes(table_name)
            self._refresh_primary_key_lookup(table_name)
            raise

        return [
            self._stored_row_to_python_dict(
                storage_row,
                table_name=table_name,
                decrypt=True,
            )
            for storage_row in storage_rows
        ]

    def update(
        self,
        table_name: str,
        values: dict[str, Any],
        where: Callable[[dict[str, Any]], bool] | None = None,
        allow_all: bool = False,
    ) -> int:
        """
        Updates rows in a table.

        Encrypted columns are decrypted for validation, then encrypted again before
        the updated rows are stored.
        """

        self._validate_existing_table(table_name)
        self._validate_row_dict(values, "values")
        self._validate_where_or_allow_all(where, allow_all, "update")

        if not values:
            raise ValueError("values cannot be empty.")

        self._validate_known_columns(
            table_name=table_name,
            values=values,
            context="update",
        )

        self._validate_update_columns_are_mutable(
            table_name=table_name,
            values=values,
        )

        matching_indices = self._matching_row_indices(
            table_name=table_name,
            where=where,
        )

        if not matching_indices:
            return 0

        updated_rows_by_index: dict[int, dict[str, Any]] = {}

        for row_index in matching_indices:
            current_row = self.data[table_name][row_index]

            candidate_public_row = self._stored_row_to_python_dict(
                current_row,
                table_name=table_name,
                decrypt=True,
            )

            for column_name, value in values.items():
                candidate_public_row[column_name] = self._clone_default(value)

            self._validate_complete_row_against_schema(
                table_name=table_name,
                row=candidate_public_row,
            )

            self._validate_primary_key_for_candidate(
                table_name=table_name,
                candidate_row=candidate_public_row,
                ignore_row_index=row_index,
            )

            self._validate_foreign_keys_for_row(
                table_name=table_name,
                row=candidate_public_row,
            )

            candidate_storage_row = self._encrypt_row_for_storage(
                table_name=table_name,
                row=candidate_public_row,
            )

            candidate_storage_row[INTERNAL_ROW_ID] = current_row[INTERNAL_ROW_ID]
            updated_rows_by_index[row_index] = candidate_storage_row

        old_rows = copy.deepcopy(self.data[table_name])
        old_next_row_id = self._next_row_id.get(table_name)
        old_row_positions = copy.deepcopy(self._row_positions.get(table_name, {}))

        try:
            for row_index, candidate_storage_row in updated_rows_by_index.items():
                self.data[table_name][row_index] = candidate_storage_row

            self._rebuild_table_indexes(table_name)
            self._refresh_primary_key_lookup(table_name)

        except Exception:
            self.data[table_name] = old_rows

            if old_next_row_id is not None:
                self._next_row_id[table_name] = old_next_row_id

            self._row_positions[table_name] = old_row_positions
            self._rebuild_table_indexes(table_name)
            self._refresh_primary_key_lookup(table_name)
            raise

        return len(updated_rows_by_index)

    def delete(
            self,
            table_name: str,
            where: Callable[[dict[str, Any]], bool] | None = None,
            allow_all: bool = False,
    ) -> int:
        """
        Deletes rows from a table.

        Behavior:
        - If other tables reference deleted rows, on_delete rules are applied:
            - RESTRICT: deletion is blocked.
            - CASCADE: referencing rows are also deleted.
            - SET NULL: referencing foreign key values are set to None.
        - Indexes are rebuilt after successful deletion.
        - If index rebuild fails, the deletion is rolled back.

        Safety:
            If where is None, delete() refuses to delete all rows unless
            allow_all=True is explicitly provided.

        Returns:
            Total number of rows deleted, including rows deleted by CASCADE.
        """

        self._validate_existing_table(table_name)
        self._validate_where_or_allow_all(where, allow_all, "delete")

        matching_indices = set(
            self._matching_row_indices(
                table_name=table_name,
                where=where,
            )
        )

        if not matching_indices:
            return 0

        delete_plan: dict[str, set[int]] = {}
        set_null_actions: set[tuple[str, int, str]] = set()

        # Important:
        # This stage only builds the plan.
        # If RESTRICT blocks deletion, it should fail here before any data changes.
        self._collect_delete_plan(
            table_name=table_name,
            row_indices=matching_indices,
            delete_plan=delete_plan,
            set_null_actions=set_null_actions,
        )

        # From here we are about to mutate actual storage.
        # Save full DB state because CASCADE / SET NULL may affect multiple tables.
        old_data = copy.deepcopy(self.data)
        old_next_row_id = copy.deepcopy(self._next_row_id)
        old_row_positions = copy.deepcopy(self._row_positions)

        try:
            # Apply SET NULL actions first, but only for rows that are not also
            # being deleted.
            for source_table, row_index, local_column in set_null_actions:
                if row_index in delete_plan.get(source_table, set()):
                    continue

                self.data[source_table][row_index][local_column] = None

            deleted_count = 0

            # Delete rows from each table.
            # Delete by descending index so list positions remain valid.
            for target_table, row_indices in delete_plan.items():
                for row_index in sorted(row_indices, reverse=True):
                    del self.data[target_table][row_index]
                    deleted_count += 1

            # Rebuild all indexes because delete may affect multiple tables.
            # This also refreshes row_id -> position mappings.
            self._rebuild_all_indexes()
            self._refresh_all_primary_key_lookups()

        except Exception:
            # Rollback all affected data and index storage metadata.
            self.data = old_data
            self._next_row_id = old_next_row_id
            self._row_positions = old_row_positions

            # Restore index maps to match the restored data.
            self._rebuild_all_indexes()
            self._refresh_all_primary_key_lookups()

            raise

        return deleted_count

    # -------------------------------------------------------------------------
    # Internal data helpers
    # -------------------------------------------------------------------------

    def _validate_row_dict(
        self,
        value: Any,
        name: str,
    ) -> None:
        """
        Validates that an input row/update object is a dictionary.

        Args:
            value:
                Value to validate.

            name:
                Human-readable name for error messages.
        """

        if not isinstance(value, dict):
            raise TypeError(f"{name} must be a dictionary.")

        for key in value:
            if not isinstance(key, str):
                raise TypeError(f"All keys in {name} must be strings.")

    def _validate_known_columns(
        self,
        table_name: str,
        values: dict[str, Any],
        context: str,
    ) -> None:
        """
        Validates that all provided columns exist in the table.

        Args:
            table_name:
                Target table.

            values:
                Dictionary of provided column values.

            context:
                Human-readable operation name, such as "insert" or "update".
        """

        table_ref = self.schema[table_name]

        unknown_columns = [
            column_name
            for column_name in values
            if column_name not in table_ref.columns
        ]

        if unknown_columns:
            raise ValueError(
                f"Unknown columns in {context} for table '{table_name}': "
                f"{unknown_columns}."
            )

    def _build_insert_row(
        self,
        table_name: str,
        input_row: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """
        Builds a complete row for insertion.

        This method fills missing values using:
        - AutoNumber generation.
        - Column defaults.
        - None for nullable columns.

        It does not commit AutoNumber sequence changes.
        Instead, it returns pending sequence updates so insert() can commit them
        only after all validation succeeds.

        Args:
            table_name:
                Target table.

            input_row:
                User-provided row.

        Returns:
            A tuple:
                (complete_row, pending_auto_sequences)
        """

        self._validate_known_columns(
            table_name=table_name,
            values=input_row,
            context="insert",
        )

        table_ref = self.schema[table_name]

        complete_row: dict[str, Any] = {}
        pending_auto_sequences: dict[str, int] = {}

        for column_name, column_def in table_ref.columns.items():
            if column_name in input_row:
                value = input_row[column_name]

                # If the user manually provides an AutoNumber value, accept it
                # as long as it is valid, and advance the internal sequence if
                # needed.
                if column_def.is_auto_number and value is not None:
                    if not self._is_value_compatible(value, int):
                        raise TypeError(
                            f"AutoNumber column '{table_name}.{column_name}' "
                            f"must receive an int value, got {type(value).__name__}."
                        )

                    current_sequence = table_ref.auto_sequences.get(column_name, 0)
                    pending_auto_sequences[column_name] = max(
                        current_sequence,
                        value,
                    )

                complete_row[column_name] = self._clone_default(value)
                continue

            if column_def.is_auto_number:
                current_sequence = table_ref.auto_sequences.get(column_name, 0)
                next_value = current_sequence + 1

                complete_row[column_name] = next_value
                pending_auto_sequences[column_name] = next_value
                continue

            if column_def.has_default:
                complete_row[column_name] = self._clone_default(column_def.default)
                continue

            if column_def.nullable:
                complete_row[column_name] = None
                continue

            raise ValueError(
                f"Missing required value for non-nullable column "
                f"'{table_name}.{column_name}'."
            )

        return complete_row, pending_auto_sequences

    def _validate_complete_row_against_schema(
        self,
        table_name: str,
        row: dict[str, Any],
    ) -> None:
        """
        Validates a complete row against the table schema.

        A complete row is expected to contain every column in the table.

        Args:
            table_name:
                Target table.

            row:
                Complete row to validate.
        """

        table_ref = self.schema[table_name]

        expected_columns = set(table_ref.columns)
        actual_columns = set(row)

        if expected_columns != actual_columns:
            missing = sorted(expected_columns - actual_columns)
            extra = sorted(actual_columns - expected_columns)

            raise ValueError(
                f"Row for table '{table_name}' does not match the table schema. "
                f"Missing columns: {missing}. Extra columns: {extra}."
            )

        for column_name, column_def in table_ref.columns.items():
            self._validate_value_for_column(
                table_name=table_name,
                column_def=column_def,
                value=row[column_name],
            )

    def _validate_value_for_column(
        self,
        table_name: str,
        column_def: ColumnDef,
        value: Any,
    ) -> None:
        """
        Validates a single value against a column definition.

        Rules:
        - None is allowed only if the column is nullable.
        - Non-None values must match the column storage type.
        """

        if value is None:
            if not column_def.nullable:
                raise ValueError(
                    f"Column '{table_name}.{column_def.name}' cannot be None."
                )
            return

        if not self._is_value_compatible(value, column_def.storage_type):
            raise TypeError(
                f"Column '{table_name}.{column_def.name}' expects "
                f"{column_def.storage_type.__name__}, got {type(value).__name__}."
            )

    def _primary_key_tuple(
        self,
        table_name: str,
        row: dict[str, Any],
    ) -> tuple[Any, ...] | None:
        """
        Returns the primary key tuple for a row.

        Args:
            table_name:
                Target table.

            row:
                Row from which to extract the primary key.

        Returns:
            Primary key tuple, or None if the table has no primary key.
        """

        primary_key = self.schema[table_name].primary_key

        if not primary_key:
            return None

        return tuple(row.get(column_name) for column_name in primary_key)

    def _validate_primary_key_for_candidate(
        self,
        table_name: str,
        candidate_row: dict[str, Any],
        ignore_row_index: int | None = None,
    ) -> None:
        """
        Validates that a candidate row does not violate primary key rules.

        Fast path:
            For insert-like operations, this uses the primary-key lookup cache.

        Fallback:
            For update() with ignore_row_index, this scans so the row does not
            conflict with itself.
        """

        key = self._primary_key_tuple(table_name, candidate_row)

        if key is None:
            return

        if any(value is None for value in key):
            raise ValueError(
                f"Primary key for table '{table_name}' cannot contain None. "
                f"Candidate key: {key}."
            )

        if ignore_row_index is None:
            lookup = self._primary_key_lookup_for_table(table_name)

            if key in lookup:
                raise ValueError(
                    f"Duplicate primary key in table '{table_name}': {key}."
                )

            return

        for row_index, existing_row in enumerate(self.data[table_name]):
            if row_index == ignore_row_index:
                continue

            existing_key = self._primary_key_tuple(table_name, existing_row)

            if existing_key == key:
                raise ValueError(
                    f"Duplicate primary key in table '{table_name}': {key}."
                )

    def _validate_primary_keys_for_batch(
        self,
        table_name: str,
        candidate_rows: list[dict[str, Any]],
    ) -> None:
        """
        Validates primary-key uniqueness for a batch before committing it.
        """

        primary_key = self.schema[table_name].primary_key

        if not primary_key:
            return

        existing_lookup = self._primary_key_lookup_for_table(table_name)
        seen_keys: set[tuple[Any, ...]] = set()

        for candidate_row in candidate_rows:
            key = self._primary_key_tuple(table_name, candidate_row)

            if key is None:
                continue

            if any(value is None for value in key):
                raise ValueError(
                    f"Primary key for table '{table_name}' cannot contain None. "
                    f"Candidate key: {key}."
                )

            if key in existing_lookup or key in seen_keys:
                raise ValueError(
                    f"Duplicate primary key in table '{table_name}': {key}."
                )

            seen_keys.add(key)

    def _validate_foreign_keys_for_row(
        self,
        table_name: str,
        row: dict[str, Any],
    ) -> None:
        """
        Validates all foreign keys for a row.

        Rules:
        - None is allowed for nullable foreign keys.
        - Non-None foreign key values must exist in the referenced table.
        """

        table_ref = self.schema[table_name]

        for local_column, foreign_key in table_ref.foreign_keys.items():
            value = row[local_column]

            if value is None:
                continue

            if not self._foreign_key_value_exists(
                target_table=foreign_key.target_table,
                target_column=foreign_key.target_column,
                value=value,
            ):
                raise ValueError(
                    f"Foreign key violation in '{table_name}.{local_column}': "
                    f"value {value!r} does not exist in "
                    f"'{foreign_key.target_table}.{foreign_key.target_column}'."
                )

    def _validate_update_columns_are_mutable(
        self,
        table_name: str,
        values: dict[str, Any],
    ) -> None:
        """
        Validates that the requested update does not modify protected columns.

        Current implementation:
        - AutoNumber columns cannot be updated.
        - Primary key columns cannot be updated.

        This keeps update() simple and prevents broken references.
        """

        table_ref = self.schema[table_name]

        for column_name in values:
            column_def = table_ref.columns[column_name]

            if column_def.is_auto_number:
                raise ValueError(
                    f"AutoNumber column '{table_name}.{column_name}' cannot be updated."
                )

            if column_def.is_primary_key:
                raise ValueError(
                    f"Primary key column '{table_name}.{column_name}' cannot be updated "
                    "in the current implementation."
                )

    def _validate_where_or_allow_all(
        self,
        where: Callable[[dict[str, Any]], bool] | None,
        allow_all: bool,
        operation: str,
    ) -> None:
        """
        Validates mass update/delete safety.

        Args:
            where:
                Optional predicate.

            allow_all:
                Whether operating on all rows is explicitly allowed.

            operation:
                Operation name for error messages.
        """

        self._validate_bool(allow_all, "allow_all")

        if where is None and not allow_all:
            raise ValueError(
                f"{operation}() without a where predicate would affect all rows. "
                "Pass allow_all=True if this is intentional."
            )

        if where is not None and not callable(where):
            raise TypeError("where must be callable or None.")

    def _matching_row_indices(
        self,
        table_name: str,
        where: Callable[[dict[str, Any]], bool] | None,
    ) -> list[int]:
        """
        Returns row indices matching a predicate.

        The predicate receives a public/decrypted row so encrypted payloads and
        RelPy internal metadata are never exposed to user callbacks.
        """

        matching_indices: list[int] = []

        for row_index, row in enumerate(self.data[table_name]):
            if where is None:
                matching_indices.append(row_index)
                continue

            public_row = self._stored_row_to_python_dict(
                row,
                table_name=table_name,
                decrypt=True,
            )

            result = where(copy.deepcopy(public_row))

            if type(result) is not bool:
                raise TypeError("where predicate must return a bool for every row.")

            if result:
                matching_indices.append(row_index)

        return matching_indices

    def _collect_delete_plan(
        self,
        table_name: str,
        row_indices: set[int],
        delete_plan: dict[str, set[int]],
        set_null_actions: set[tuple[str, int, str]],
    ) -> None:
        """
        Recursively builds a deletion plan according to foreign key rules.

        Args:
            table_name:
                Table from which rows are being deleted.

            row_indices:
                Indices of rows to delete from table_name.

            delete_plan:
                Mutable dictionary mapping table names to row indices that
                should be deleted.

            set_null_actions:
                Mutable set of actions:
                    (source_table, row_index, local_column)

                These actions are applied before deletion.
        """

        if not row_indices:
            return

        planned_for_table = delete_plan.setdefault(table_name, set())
        new_indices = row_indices - planned_for_table

        if not new_indices:
            return

        planned_for_table.update(new_indices)

        # For every foreign key in the schema, check whether it references
        # rows that are now planned for deletion.
        for source_table_name, source_table in self.schema.items():
            for foreign_key in source_table.foreign_keys.values():
                if foreign_key.target_table != table_name:
                    continue

                target_values = {
                    self.data[table_name][row_index].get(foreign_key.target_column)
                    for row_index in new_indices
                }

                dependent_indices = {
                    row_index
                    for row_index, row in enumerate(self.data[source_table_name])
                    if row.get(foreign_key.local_column) in target_values
                }

                already_planned = delete_plan.get(source_table_name, set())
                dependent_indices = dependent_indices - already_planned

                if not dependent_indices:
                    continue

                if foreign_key.on_delete == "RESTRICT":
                    raise ValueError(
                        f"Cannot delete from '{table_name}' because rows in "
                        f"'{source_table_name}' reference it through foreign key "
                        f"'{source_table_name}.{foreign_key.local_column}' -> "
                        f"'{foreign_key.target_table}.{foreign_key.target_column}' "
                        "with ON DELETE RESTRICT."
                    )

                if foreign_key.on_delete == "CASCADE":
                    self._collect_delete_plan(
                        table_name=source_table_name,
                        row_indices=dependent_indices,
                        delete_plan=delete_plan,
                        set_null_actions=set_null_actions,
                    )
                    continue

                if foreign_key.on_delete == "SET NULL":
                    for dependent_index in dependent_indices:
                        set_null_actions.add(
                            (
                                source_table_name,
                                dependent_index,
                                foreign_key.local_column,
                            )
                        )
                    continue

                raise ValueError(
                    f"Unsupported on_delete rule: {foreign_key.on_delete}."
                )
